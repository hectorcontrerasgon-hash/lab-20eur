#!/usr/bin/env python3
"""
Laboratorio 20 EUR - bot de la Fase 1 (paper trading en Alpaca).

REGLA CONGELADA (no se toca durante las 12 semanas de prueba):
  - Activo: BTC/USD.
  - Una vez al dia se toma el ultimo cierre diario COMPLETO y se compara con sus medias
    moviles simples (SMA) de 50, 100, 150 y 200 dias.
  - Peso objetivo = proporcion de medias que el precio supera: 0, 25, 50, 75 o 100 %.
  - Solo se opera cuando cambia el peso objetivo (no hay reequilibrios diarios).
  - El laboratorio es una sub-cartera virtual de LAB_CAPITAL_USD (unos 20 EUR) dentro de la
    cuenta de papel, que trae 100.000 USD ficticios. El bot solo mueve lo suyo.

SEGURIDAD:
  - Solo habla con la API de PAPEL (paper-api.alpaca.markets). No hay dinero real.
  - Las claves se leen de variables de entorno (Secrets de GitHub o archivo .env) y nunca se imprimen.
  - Si existe un archivo llamado STOP en la raiz del repositorio, no se envian ordenes.
  - Una sola ejecucion por dia (UTC) y client_order_id unico: repetir el trabajo no duplica ordenes.

MODELO EN VIVO:
  Cada dia se recalcula que habria hecho la regla "de libro" (operando justo al cierre, con la
  comision supuesta) desde el primer dia. La diferencia entre el laboratorio y ese modelo mide si
  el sistema hace lo que dice el backtest. Ese es el criterio de la Fase 1, no el beneficio.
"""
from __future__ import annotations

import csv
import json
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_DOWN
from pathlib import Path
from typing import Callable, Optional

try:
    import requests
except ImportError:  # los tests con cliente falso no necesitan requests
    requests = None

SYMBOL = "BTC/USD"
POSITION_SYMBOLS = ("BTCUSD", "BTC/USD")
WINDOWS = (50, 100, 150, 200)
TRADING_URL = "https://paper-api.alpaca.markets"  # SOLO papel en la Fase 1
DATA_URL = "https://data.alpaca.markets"
FINAL_STATUSES = {"filled", "canceled", "expired", "rejected", "done_for_day"}

LOG_FIELDS = [
    "ejecucion_utc", "cierre_fecha", "cierre_usd", "sma50", "sma100", "sma150", "sma200",
    "peso_anterior", "peso_objetivo", "accion", "cantidad_btc", "precio_ejecucion_usd",
    "comision_usd", "efectivo_usd", "btc", "precio_valoracion_usd", "capital_usd",
    "capital_al_cierre_usd", "modelo_al_cierre_usd", "diferencia_vs_modelo_pct", "nota",
]


class LabError(Exception):
    """Error controlado: se registra y el trabajo termina en rojo."""


@dataclass
class Config:
    capital_usd: float = 23.20       # ~20 EUR a 1,16 USD/EUR
    fee_rate: float = 0.0025         # comision supuesta por orden (tramo 1 de Alpaca, tomador)
    min_order_usd: float = 1.0       # Alpaca no acepta ordenes cripto de menos de ~1 USD
    data_dir: Path = Path("data")
    stop_file: Path = Path("STOP")
    dry_run: bool = False
    force: bool = False
    poll_attempts: int = 20
    sleep: Callable[[float], None] = field(default=time.sleep, repr=False)

    @classmethod
    def from_env(cls) -> "Config":
        truthy = lambda v: str(v).strip().lower() in {"1", "true", "yes", "si", "sí"}
        return cls(
            capital_usd=float(os.getenv("LAB_CAPITAL_USD", "23.20")),
            fee_rate=float(os.getenv("LAB_FEE_RATE", "0.0025")),
            data_dir=Path(os.getenv("LAB_DATA_DIR", "data")),
            stop_file=Path(os.getenv("LAB_STOP_FILE", "STOP")),
            dry_run=truthy(os.getenv("DRY_RUN", "0")),
            force=truthy(os.getenv("LAB_FORCE", "0")),
        )


# ----------------------------------------------------------------------------------------------
# Funciones puras (las mismas que el backtest; se prueban sin red)
# ----------------------------------------------------------------------------------------------
def parse_ts(value: str) -> datetime:
    value = value.replace("Z", "+00:00")
    if "." in value:  # Alpaca puede mandar nanosegundos; Python solo admite microsegundos
        head, rest = value.split(".", 1)
        frac = "".join(ch for ch in rest if ch.isdigit())
        tz = rest[len(frac):]
        value = f"{head}.{frac[:6]}{tz}"
    dt = datetime.fromisoformat(value)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def complete_daily_closes(bars: list[dict], now: datetime) -> list[tuple[str, float]]:
    """Solo velas diarias ya cerradas (inicio + 24 h <= ahora), sea cual sea su hora de corte."""
    out = []
    for bar in sorted(bars, key=lambda b: b["t"]):
        start = parse_ts(bar["t"])
        if start + timedelta(days=1) <= now:
            out.append((start.date().isoformat(), float(bar["c"])))
    return out


def smas(closes: list[float]) -> dict[int, float]:
    if len(closes) < max(WINDOWS):
        raise LabError(f"Hacen falta {max(WINDOWS)} cierres diarios y solo hay {len(closes)}.")
    return {n: sum(closes[-n:]) / n for n in WINDOWS}


def target_weight(closes: list[float]) -> float:
    last = closes[-1]
    medias = smas(closes)
    return sum(1 for n in WINDOWS if last > medias[n]) / len(WINDOWS)


def plan_trade(cash: float, qty: float, price: float, weight: float, fee: float) -> tuple[Optional[str], float]:
    """Cuanto BTC comprar o vender para quedar en 'weight' del capital del laboratorio."""
    equity = cash + qty * price
    target_qty = weight * equity / price
    if weight <= 0:
        return ("sell", qty) if qty > 0 else (None, 0.0)
    if target_qty > qty:
        spend = min((target_qty - qty) * price, cash / (1 + fee))
        return ("buy", max(spend, 0.0) / price)
    if target_qty < qty:
        return ("sell", qty - target_qty)
    return (None, 0.0)


def apply_fill(cash: float, qty: float, side: str, fill_qty: float, fill_price: float, fee: float) -> tuple[float, float, float]:
    notional = fill_qty * fill_price
    fee_usd = notional * fee
    if side == "buy":
        return cash - notional - fee_usd, qty + fill_qty, fee_usd
    return cash + notional - fee_usd, qty - fill_qty, fee_usd


def simulate_model(history: list[tuple[str, float]], start_date: str, capital: float, fee: float) -> dict:
    """La regla 'de libro': decide y opera en cada cierre desde start_date, sin minimos ni retrasos."""
    closes = [c for _, c in history]
    cash, qty, prev_w, orders = capital, 0.0, None, 0
    for i, (date, price) in enumerate(history):
        if date < start_date:
            continue
        w = target_weight(closes[: i + 1])
        if prev_w is None or w != prev_w:
            side, trade_qty = plan_trade(cash, qty, price, w, fee)
            if side:
                cash, qty, _ = apply_fill(cash, qty, side, trade_qty, price, fee)
                orders += 1
            prev_w = w
    last_price = closes[-1]
    return {"equity": cash + qty * last_price, "cash": cash, "qty": qty, "orders": orders, "weight": prev_w}


def quantize_down(value: float, increment: str) -> Decimal:
    inc = Decimal(str(increment))
    return (Decimal(str(value)) / inc).to_integral_value(rounding=ROUND_DOWN) * inc


# ----------------------------------------------------------------------------------------------
# Cliente de Alpaca (solo papel)
# ----------------------------------------------------------------------------------------------
class AlpacaPaperClient:
    def __init__(self, key_id: str, secret_key: str, timeout: int = 20):
        if requests is None:
            raise LabError("Falta la libreria 'requests' (pip install -r requirements.txt).")
        self.session = requests.Session()
        self.session.headers.update({
            "APCA-API-KEY-ID": key_id,
            "APCA-API-SECRET-KEY": secret_key,
            "Accept": "application/json",
        })
        self.timeout = timeout

    def _request(self, method: str, url: str, **kwargs):
        for attempt in range(4):
            try:
                resp = self.session.request(method, url, timeout=self.timeout, **kwargs)
            except requests.RequestException as exc:
                if attempt == 3:
                    raise LabError(f"Sin conexion con Alpaca ({type(exc).__name__}).") from None
                time.sleep(2 ** attempt)
                continue
            if resp.status_code in (429, 500, 502, 503, 504) and attempt < 3:
                time.sleep(2 ** attempt)
                continue
            return resp
        raise LabError("Alpaca no responde.")

    def _json(self, method: str, url: str, ok404: bool = False, **kwargs):
        resp = self._request(method, url, **kwargs)
        if ok404 and resp.status_code == 404:
            return None
        if resp.status_code in (401, 403):
            raise LabError("Alpaca rechaza las claves (401/403). Revisa los Secrets ALPACA_KEY_ID y "
                           "ALPACA_SECRET_KEY: tienen que ser las claves de la cuenta Paper.")
        if resp.status_code >= 400:
            path = url.split(".markets", 1)[-1].split("?", 1)[0]
            raise LabError(f"{method} {path} -> {resp.status_code}: {resp.text[:300]}")
        return resp.json()

    def account(self) -> dict:
        return self._json("GET", f"{TRADING_URL}/v2/account")

    def asset(self) -> dict:
        for sym in ("BTCUSD", "BTC%2FUSD"):
            data = self._json("GET", f"{TRADING_URL}/v2/assets/{sym}", ok404=True)
            if data:
                return data
        raise LabError("Alpaca no encuentra el activo BTC/USD.")

    def position_qty(self) -> float:
        for pos in self._json("GET", f"{TRADING_URL}/v2/positions") or []:
            if pos.get("symbol") in POSITION_SYMBOLS:
                return float(pos.get("qty_available") or pos.get("qty") or 0)
        return 0.0

    def daily_bars(self, start: str, end: str) -> list[dict]:
        bars, token = [], None
        while True:
            params = {"symbols": SYMBOL, "timeframe": "1Day", "start": start, "end": end,
                      "limit": 10000, "sort": "asc"}
            if token:
                params["page_token"] = token
            data = self._json("GET", f"{DATA_URL}/v1beta3/crypto/us/bars", params=params)
            bars.extend((data.get("bars") or {}).get(SYMBOL, []))
            token = data.get("next_page_token")
            if not token:
                return bars

    def latest_price(self) -> float:
        data = self._json("GET", f"{DATA_URL}/v1beta3/crypto/us/latest/quotes", params={"symbols": SYMBOL})
        quote = (data.get("quotes") or {}).get(SYMBOL) or {}
        bid, ask = float(quote.get("bp") or 0), float(quote.get("ap") or 0)
        if bid > 0 and ask > 0:
            return (bid + ask) / 2
        if max(bid, ask) > 0:
            return max(bid, ask)
        raise LabError("Alpaca no devuelve precio actual de BTC/USD.")

    def order_by_client_id(self, client_order_id: str) -> Optional[dict]:
        return self._json("GET", f"{TRADING_URL}/v2/orders:by_client_order_id",
                          ok404=True, params={"client_order_id": client_order_id})

    def get_order(self, order_id: str) -> dict:
        return self._json("GET", f"{TRADING_URL}/v2/orders/{order_id}")

    def submit_market_order(self, side: str, qty: str, client_order_id: str) -> dict:
        body = {"symbol": SYMBOL, "qty": qty, "side": side, "type": "market",
                "time_in_force": "gtc", "client_order_id": client_order_id}
        return self._json("POST", f"{TRADING_URL}/v2/orders", json=body)


# ----------------------------------------------------------------------------------------------
# Estado y registro
# ----------------------------------------------------------------------------------------------
def load_state(cfg: Config) -> Optional[dict]:
    path = cfg.data_dir / "estado.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def save_state(cfg: Config, state: dict) -> None:
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    (cfg.data_dir / "estado.json").write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")


def append_log(cfg: Config, row: dict) -> None:
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    path = cfg.data_dir / "registro.csv"
    new = not path.exists()
    with path.open("a", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=LOG_FIELDS)
        if new:
            writer.writeheader()
        writer.writerow({k: row.get(k, "") for k in LOG_FIELDS})


def read_log(cfg: Config) -> list[dict]:
    path = cfg.data_dir / "registro.csv"
    if not path.exists():
        return []
    with path.open(encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def madrid(now: datetime) -> str:
    try:
        from zoneinfo import ZoneInfo
        return now.astimezone(ZoneInfo("Europe/Madrid")).strftime("%d/%m/%Y %H:%M") + " (Madrid)"
    except Exception:
        return now.strftime("%d/%m/%Y %H:%M") + " (UTC)"


def fmt(x: float, d: int = 2) -> str:
    return f"{x:,.{d}f}".replace(",", "X").replace(".", ",").replace("X", ".")


def num(value, d: int = 2) -> str:
    try:
        return fmt(float(value), d)
    except (TypeError, ValueError):
        return "-"


def write_summary(cfg: Config, state: dict, row: dict, now: datetime) -> str:
    rows = read_log(cfg)[-10:]
    capital = float(row["capital_usd"])
    start = state["capital_inicial_usd"]
    detalle = row["accion"]
    if row.get("cantidad_btc"):
        detalle += f" {row['cantidad_btc']} BTC"
    if row.get("precio_ejecucion_usd"):
        detalle += f" a {num(row['precio_ejecucion_usd'])} USD"
    if row.get("nota"):
        detalle += f" ({row['nota']})"
    lines = [
        "# Laboratorio 20 EUR - estado",
        "",
        f"Actualizado: {madrid(now)}",
        "",
        "| | |",
        "|---|---|",
        "| Regla (congelada) | Media de SMA50/100/150/200 sobre BTC/USD, cuenta de papel |",
        f"| Exposicion actual | {int(round(float(state['weight'] or 0) * 100))} % en BTC |",
        f"| Capital del laboratorio | {fmt(capital)} USD (inicio {fmt(start)} USD, {fmt((capital / start - 1) * 100, 1)} %) |",
        f"| Modelo de libro al cierre | {num(row['modelo_al_cierre_usd'])} USD (diferencia {num(row['diferencia_vs_modelo_pct'])} %) |",
        f"| Ordenes / comisiones | {state['n_orders']} / {fmt(state['fees_usd'], 4)} USD |",
        f"| En marcha desde | {state['start_date_utc']} |",
        f"| Ultima accion | {detalle} |",
        "",
        "## Ultimas ejecuciones",
        "",
        "| Fecha (UTC) | Cierre USD | Peso | Accion | Capital USD | vs modelo % |",
        "|---|---|---|---|---|---|",
    ]
    for r in rows:
        peso = r["peso_objetivo"]
        peso_txt = f"{int(round(float(peso) * 100))} %" if peso not in ("", None) else "-"
        lines.append(f"| {r['ejecucion_utc'][:16].replace('T', ' ')} | {num(r['cierre_usd'])} | {peso_txt} | "
                     f"{r['accion']} | {num(r['capital_usd'])} | {num(r['diferencia_vs_modelo_pct'])} |")
    text = "\n".join(lines) + "\n"
    (cfg.data_dir / "ESTADO.md").write_text(text, encoding="utf-8")
    return text


# ----------------------------------------------------------------------------------------------
# Una ejecucion diaria
# ----------------------------------------------------------------------------------------------
def execute_order(client, cfg: Config, side: str, qty_str: str, client_order_id: str) -> dict:
    order = client.order_by_client_id(client_order_id)
    if order is None:
        order = client.submit_market_order(side, qty_str, client_order_id)
    for _ in range(cfg.poll_attempts):
        if order.get("status") in FINAL_STATUSES:
            break
        cfg.sleep(1.5)
        order = client.get_order(order["id"])
    return order


def run_once(client, cfg: Config, now: Optional[datetime] = None) -> dict:
    now = now or datetime.now(timezone.utc)
    today = now.date().isoformat()
    state = load_state(cfg)

    if state and state.get("last_run_date_utc") == today and not cfg.force:
        print(f"Ya se ejecuto hoy ({today} UTC). No se hace nada.")
        return {"accion": "ya_ejecutado"}

    account = client.account()
    if str(account.get("status", "ACTIVE")).upper() != "ACTIVE":
        raise LabError(f"La cuenta de papel no esta activa (status={account.get('status')}).")
    if "crypto_status" in account and str(account["crypto_status"]).upper() != "ACTIVE":
        raise LabError(f"La cuenta de papel no tiene cripto activa (crypto_status={account['crypto_status']}).")

    # Historial suficiente para las medias de HOY y para recalcular el modelo desde el primer dia
    fetch_from = (now - timedelta(days=330)).date()
    if state and state.get("start_bar_date"):
        fetch_from = min(fetch_from, datetime.fromisoformat(state["start_bar_date"]).date() - timedelta(days=300))
    bars = client.daily_bars(fetch_from.isoformat(), now.strftime("%Y-%m-%dT%H:%M:%SZ"))
    history = complete_daily_closes(bars, now)
    closes = [c for _, c in history]
    medias = smas(closes)
    weight = target_weight(closes)
    close_date, close = history[-1]
    price = client.latest_price()

    if state is None:
        state = {
            "version": 1,
            "start_date_utc": today,
            "start_bar_date": close_date,
            "capital_inicial_usd": cfg.capital_usd,
            "cash_usd": cfg.capital_usd,
            "btc_qty": 0.0,
            "weight": None,
            "last_run_date_utc": None,
            "n_orders": 0,
            "fees_usd": 0.0,
            "pending_order": None,
        }

    prev_weight = state["weight"]
    action, trade_qty, fill_price, fee_usd, note = "mantener", 0.0, "", 0.0, ""

    # 1) Orden pendiente de un dia anterior (raro en cripto a mercado, pero se contempla)
    pending = state.get("pending_order")
    if pending and not cfg.dry_run:
        order = client.get_order(pending["id"])
        filled = float(order.get("filled_qty") or 0) - float(pending.get("applied_qty", 0))
        if filled > 0:
            cash, qty, f = apply_fill(state["cash_usd"], state["btc_qty"], pending["side"], filled,
                                      float(order["filled_avg_price"]), cfg.fee_rate)
            state.update(cash_usd=cash, btc_qty=qty)
            state["fees_usd"] += f
            pending["applied_qty"] = float(order.get("filled_qty") or 0)
        if order.get("status") in FINAL_STATUSES:
            state["pending_order"] = None
            state["weight"] = pending["weight"] if order.get("status") == "filled" else state["weight"]
            note = f"orden pendiente cerrada ({order.get('status')})"
        else:
            note = "sigue habiendo una orden pendiente; hoy no se opera"
        save_state(cfg, state)  # guardar ya: si algo falla despues, no se pierde la ejecucion
        prev_weight = state["weight"]

    # 2) Decision del dia
    if cfg.stop_file.exists():
        action, note = "pausado", "existe el archivo STOP: no se envian ordenes"
    elif state.get("pending_order"):
        action = "esperando"
    elif prev_weight is None or weight != prev_weight:
        side, qty_float = plan_trade(state["cash_usd"], state["btc_qty"], price, weight, cfg.fee_rate)
        if side is None or qty_float * price < cfg.min_order_usd:
            action, note = "sin_orden", "el ajuste es menor que la orden minima"
            if not cfg.dry_run:
                state["weight"] = weight
        else:
            asset = client.asset()
            if not asset.get("tradable", True):
                raise LabError("BTC/USD no se puede operar ahora mismo en Alpaca.")
            increment = asset.get("min_trade_increment") or "0.000000001"
            qty_dec = quantize_down(qty_float, increment)
            if side == "sell":
                # round(...,9) quita el ruido de coma flotante; nunca se vende mas de lo que hay en el broker
                held = quantize_down(round(min(state["btc_qty"], client.position_qty()), 9), increment)
                qty_dec = min(qty_dec, held) if weight > 0 else held
            if qty_dec <= 0 or qty_dec < Decimal(str(asset.get("min_order_size") or "0")):
                action, note = "sin_orden", "la cantidad queda por debajo del minimo de Alpaca"
                if not cfg.dry_run:
                    state["weight"] = weight
            elif cfg.dry_run:
                action, trade_qty, note = f"PRUEBA_{side}", float(qty_dec), "modo prueba: no se envia la orden"
            else:
                cid = f"lab20-{today}-{side}-{int(weight * 100)}"
                order = execute_order(client, cfg, side, format(qty_dec, "f"), cid)
                filled = float(order.get("filled_qty") or 0)
                if filled > 0:
                    fill_price = float(order["filled_avg_price"])
                    cash, qty, fee_usd = apply_fill(state["cash_usd"], state["btc_qty"], side, filled,
                                                    fill_price, cfg.fee_rate)
                    state.update(cash_usd=cash, btc_qty=qty)
                    state["fees_usd"] += fee_usd
                    state["n_orders"] += 1
                    trade_qty = filled
                if order.get("status") == "filled":
                    action = "compra" if side == "buy" else "venta"
                    state["weight"] = weight
                    if weight == 0:  # salida total: no arrastrar polvo contable
                        residual, state["btc_qty"] = state["btc_qty"], 0.0
                        if residual >= 1e-9:
                            note = f"residuo de {residual:.9f} BTC ({residual * price:.4f} USD) puesto a 0"
                elif order.get("status") in FINAL_STATUSES:
                    save_state(cfg, state)
                    raise LabError(f"Alpaca no ejecuto la orden (estado {order.get('status')}).")
                else:
                    action, note = "orden_pendiente", "la orden sigue abierta; se revisa en la proxima ejecucion"
                    state["pending_order"] = {"id": order["id"], "side": side, "weight": weight,
                                              "applied_qty": filled}
                save_state(cfg, state)  # guardar la operacion antes de nada mas

    # 3) Valoracion y comparacion con el modelo de libro
    capital = state["cash_usd"] + state["btc_qty"] * price
    capital_close = state["cash_usd"] + state["btc_qty"] * close
    model = simulate_model(history, state["start_bar_date"], state["capital_inicial_usd"], cfg.fee_rate)
    diff = (capital_close / model["equity"] - 1) * 100 if model["equity"] else 0.0

    row = {
        "ejecucion_utc": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "cierre_fecha": close_date,
        "cierre_usd": round(close, 2),
        **{f"sma{n}": round(medias[n], 2) for n in WINDOWS},
        "peso_anterior": "" if prev_weight is None else prev_weight,
        "peso_objetivo": weight,
        "accion": action,
        "cantidad_btc": f"{trade_qty:.9f}".rstrip("0").rstrip(".") if trade_qty else "",
        "precio_ejecucion_usd": round(fill_price, 2) if fill_price else "",
        "comision_usd": round(fee_usd, 6) if fee_usd else "",
        "efectivo_usd": round(state["cash_usd"], 6),
        "btc": round(state["btc_qty"], 9),
        "precio_valoracion_usd": round(price, 2),
        "capital_usd": round(capital, 4),
        "capital_al_cierre_usd": round(capital_close, 4),
        "modelo_al_cierre_usd": round(model["equity"], 4),
        "diferencia_vs_modelo_pct": round(diff, 2),
        "nota": note,
    }

    print(f"Cierre {close_date}: {fmt(close)} USD | SMA50 {fmt(medias[50])} | SMA100 {fmt(medias[100])} | "
          f"SMA150 {fmt(medias[150])} | SMA200 {fmt(medias[200])}")
    print(f"Peso objetivo {int(weight * 100)} % (antes: {'-' if prev_weight is None else int(prev_weight * 100)} %) "
          f"-> {action} {row['cantidad_btc']} {note}".rstrip())
    print(f"Capital del laboratorio: {fmt(capital, 4)} USD | modelo al cierre: {fmt(model['equity'], 4)} USD "
          f"| diferencia {fmt(diff)} %")

    if cfg.dry_run:
        print("MODO PRUEBA: conexion correcta, no se ha enviado ninguna orden ni se ha guardado nada.")
        return row

    state["last_run_date_utc"] = today
    save_state(cfg, state)
    append_log(cfg, row)
    summary = write_summary(cfg, state, row, now)
    step_summary = os.getenv("GITHUB_STEP_SUMMARY")
    if step_summary:
        with open(step_summary, "a", encoding="utf-8") as fh:
            fh.write(summary)
    return row


def load_dotenv(path: Path = Path(".env")) -> None:
    """Para ejecutarlo en el PC: lee claves de un archivo .env sin librerias extra."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def main() -> int:
    load_dotenv()
    cfg = Config.from_env()
    key_id, secret = os.getenv("ALPACA_KEY_ID", "").strip(), os.getenv("ALPACA_SECRET_KEY", "").strip()
    try:
        if not key_id or not secret:
            raise LabError("Faltan las claves: define ALPACA_KEY_ID y ALPACA_SECRET_KEY "
                           "(Secrets de GitHub o archivo .env).")
        run_once(AlpacaPaperClient(key_id, secret), cfg)
        return 0
    except Exception as exc:  # cualquier fallo se registra y el trabajo termina en rojo
        message = str(exc) if isinstance(exc, LabError) else f"{type(exc).__name__}: {exc}"
        print(f"ERROR: {message}", file=sys.stderr)
        if not cfg.dry_run:
            append_log(cfg, {"ejecucion_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                             "accion": "error", "nota": message[:300]})
        return 1


if __name__ == "__main__":
    sys.exit(main())
