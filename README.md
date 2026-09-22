# Laboratorio 20 € · Fase 1 (paper trading)

Un bot que aplica **cada día y sin intervención** una regla de tendencia sobre bitcoin en una cuenta
**de papel** de Alpaca (dinero ficticio). Dura 12 semanas. El objetivo es comprobar que el sistema
hace lo que dice el backtest: sin fallos, con las mismas señales y con un coste dentro de lo previsto.
**Ganar dinero no es el criterio**, porque en 12 semanas el resultado es ruido.

**La regla (congelada, no se toca durante la prueba)**
- Cada mañana se toma el último cierre diario completo de BTC/USD y se compara con sus medias
  móviles de 50, 100, 150 y 200 días.
- El porcentaje invertido es la proporción de medias que el precio supera: 0, 25, 50, 75 o 100 %.
- Solo se opera cuando cambia ese porcentaje. De media histórica son unas 40 órdenes pequeñas al
  año, unas 9 en las 12 semanas.
- El laboratorio es una subcartera de **23,20 USD (≈ 20 €)** dentro de la cuenta de papel, que viene
  con 100.000 USD ficticios. El bot solo mueve lo suyo.

---

## Puesta en marcha (una vez, unos 15 minutos)

### 1 · Cuenta de papel en Alpaca
1. Regístrate en <https://alpaca.markets> con tu email y confirma el correo. Para la cuenta de papel
   basta con eso, no hace falta verificar la identidad ni depositar dinero.
2. En el panel, comprueba que estás en la cuenta **Paper**.
3. En la sección **API Keys**, genera un par de claves: **Key ID** y **Secret Key**. La Secret solo
   se muestra una vez, así que no cierres la pestaña hasta terminar el paso 2.

### 2 · Repositorio en GitHub
1. Crea un repositorio nuevo llamado `lab-20eur`, **público** y con "Add a README".
   Es público para que Claude pueda leer el registro. No contiene nada sensible, porque las claves
   van aparte y cifradas.
2. En el repositorio, entra en **Settings → Secrets and variables → Actions → New repository secret**
   y crea dos secretos:
   - `ALPACA_KEY_ID`: la Key ID
   - `ALPACA_SECRET_KEY`: la Secret Key
3. Ve a **Add file → Upload files** y arrastra **todo** el contenido de esta carpeta, incluida la
   carpeta `.github`. Después pulsa **Commit changes**.
   - Si la carpeta `.github` no se sube, crea el archivo a mano: **Add file → Create new file**,
     escribe como nombre `.github/workflows/lab.yml`, pega el contenido del archivo y guarda.
   - **No subas nunca un archivo `.env`.**

### 3 · Primera ejecución
1. Abre la pestaña **Actions**. Si te pide activar los workflows, acéptalo.
2. Entra en **Laboratorio 20 EUR → Run workflow**, deja marcado **Modo prueba** y ejecútalo.
3. Cuando termine (tarda alrededor de un minuto), abre la ejecución y mira el paso
   **"Ejecutar el bot"**. Tiene que mostrar el último cierre, las cuatro medias, el peso objetivo y
   `MODO PRUEBA: conexion correcta`.
4. Para empezar ya, vuelve a pulsar **Run workflow** con **Modo prueba desmarcado**. Si no lo haces,
   el bot empieza solo al día siguiente a las 06:20 UTC (08:20 en Madrid en verano).

---

## Cómo seguirlo
- **`data/ESTADO.md`**: resumen al día (exposición, capital, diferencia con el modelo, últimas ejecuciones).
- **`data/registro.csv`**: una fila por día con el cierre, las medias, la decisión, la ejecución y la valoración.
- **En Alpaca (Paper) → Orders / Positions**: las órdenes reales de papel. Tienen que coincidir con el registro.
- Si una ejecución falla, GitHub te avisa por email y el error queda anotado en `registro.csv`.

**Modelo de libro:** cada día el bot recalcula qué habría hecho la regla "perfecta" (operando justo
al cierre y con la comisión supuesta) desde el primer día. La columna `diferencia_vs_modelo_pct`
mide cuánto se aleja la realidad del backtest. Esa es la cifra importante.

## Cómo pausarlo o pararlo
- **Pausar:** crea en el repositorio un archivo llamado `STOP` (**Add file → Create new file**).
  Mientras exista, el bot sigue anotando pero no envía órdenes. Para reanudar, bórralo.
- **Parar del todo:** ve a **Actions → Laboratorio 20 EUR → ··· → Disable workflow**.

## Criterios para pasar a la Fase 2 (al cabo de 12 semanas)
1. Ningún error sin explicar.
2. Todas las decisiones coinciden con la regla.
3. La diferencia con el modelo se explica por comisiones y deslizamiento, sin sorpresas.
4. **El beneficio no cuenta.** Con 9 operaciones es ruido, tanto si sale bien como si sale mal.

## Qué no hace (a propósito)
- No usa dinero real: solo habla con `paper-api.alpaca.markets`.
- No lee noticias ni cambia la regla sobre la marcha.
- No mueve más que el capital del laboratorio.

---

## Detalles técnicos
- **Datos:** velas diarias de Alpaca (`/v1beta3/crypto/us/bars`). Solo se usan las velas **cerradas**,
  y la vela en curso se descarta, sea cual sea su hora de corte.
- **Órdenes:** a mercado, `gtc`, con un `client_order_id` único por día. Si el trabajo se repite,
  no se duplica la orden. Solo hay una ejecución por día en UTC.
- **Comisión supuesta en la contabilidad:** 0,25 % por orden (tramo 1 de Alpaca, tomador). La cuenta
  de papel puede no cobrarla, pero el laboratorio la descuenta igualmente para ser realista.
- **Estado:** `data/estado.json`. Si lo borras, el laboratorio empieza de cero.
- **Pruebas sin red:** `python -m unittest discover -s tests -v`

## Alternativa: ejecutarlo en tu PC con Windows (usa solo una de las dos opciones)
1. Instala Python 3.12 y ejecuta `pip install -r requirements.txt` en esta carpeta.
2. Copia `.env.example` como `.env` y rellena las claves. Con `DRY_RUN=1` funciona en modo prueba.
3. Prueba con `python bot.py`. Cuando funcione, cambia a `DRY_RUN=0`.
4. Prográmalo con `schtasks /Create /SC DAILY /ST 08:30 /TN "Laboratorio20EUR" /TR "\"%CD%\run_local.bat\""`.
   En el Programador de tareas, marca "Ejecutar la tarea lo antes posible después de un inicio
   programado omitido". Ten en cuenta que solo operará cuando el PC esté encendido.
