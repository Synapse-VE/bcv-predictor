# BCV Autopilot — sistema autónomo de pronóstico

Un solo proceso vigila el BCV, detecta la publicación de la nueva tasa, puntúa su
pronóstico y se recalibra con cada dato. Expone el estado en JSON para el dashboard.

---

## Arranque

```bash
cd bcv-predictor

python3 autopilot.py run          # loop autónomo (foreground)
python3 autopilot.py once         # una sola pasada (para cron/systemd timer)
python3 autopilot.py status       # ver estado y pronóstico (no frena nada)
python3 autopilot.py status --json
python3 autopilot.py backtest     # validación histórica del modelo
python3 autopilot.py reset --yes  # borrar estado (re-siembra la historia)
```

> `status` y `backtest` funcionan incluso sin `boto3`. Para usar R2 conviene el
> Python del venv (`.venv/bin/python`).

Para dejarlo corriendo desacoplado de la terminal:

```bash
nohup python3 autopilot.py run >> state/autopilot.out 2>&1 &
```

O como servicio de usuario (`~/.config/systemd/user/bcv-autopilot.service`):

```ini
[Unit]
Description=BCV Autopilot
After=network-online.target

[Service]
WorkingDirectory=%h/bcv-predictor
ExecStart=%h/bcv-predictor/.venv/bin/python autopilot.py run
Restart=always
RestartSec=30

[Install]
WantedBy=default.target
```

```bash
systemctl --user daemon-reload
systemctl --user enable --now bcv-autopilot
journalctl --user -u bcv-autopilot -f
```

---

## Qué hace en cada pasada

```
consulta la portada del BCV (cada 3 min de 13:00 a 21:00; cada 30 min el resto)
   │
   ├─ ¿cambió la Fecha Valor?  (no se asume hora: la publicación varía 14:30–22:00)
   │      NO → no hace nada, solo actualiza last_poll
   │      SÍ → guarda el dato real
   │            puntúa el pronóstico que estaba vigente (error en Bs y %)
   │            actualiza los pesos de los 7 modelos
   │            recalibra damping (phi) y bandas
   │            lanza los pronósticos oficiales 1d / 5d / 21d
   │            registra la hora real de publicación
   └─ escribe estado con reemplazo atómico (nunca deja archivos a medias)
```

---

## Cómo se auto-corrige

1. **Pool de 7 modelos**: EWMA-3, EWMA-5, drift-3, drift-10, AR(1), “medio paso”
   y naive. Cada uno propone el retorno del próximo día.
2. **Pesos adaptativos**: inverse-MSE sobre los últimos 60 errores de cada modelo,
   con piso del 2% para que ninguno muera. El que viene acertando pesa más.
3. **Damping por horizonte** (`phi`): evita extrapolar linealmente el impulso actual
   durante una semana o un mes. Se calibra por grilla sobre los últimos 300 días.
4. **Anclaje histórico por horizonte**: para h ≥ 2, el pronóstico mezcla
   `75% impulso actual + 25% mediana histórica de los retornos a h días`
   (ventana de 120 observaciones). Mejora el sesgo del mes.
5. **Bandas empíricas**: cuantiles de `real / pronosticado` fuera de muestra.
   Se ensanchan con el horizonte y son asimétricas (el mercado tiene cola al alza).
6. **Detección de régimen**: compara volatilidad de 20 días contra 250 días.
   Se guarda en `forecast.json` y `status`.
7. **Aprendizaje en caliente**: al arrancar, `warm_start` corre los modelos sobre
   los últimos 250 días para que los pesos iniciales ya estén informados.

---

## Validación histórica (backtest)

Sobre la serie oficial de 555 días hábiles (jul-2024 → oct-2026):

| Horizonte | MAE autopilot | MAE naive | Mejora |
|---|---|---|---|
| 1 día | **0,667 Bs** (0,24%) | 1,774 Bs | **62,4%** |
| 5 días | **4,30 Bs** | 13,42 Bs | **68,0%** |
| 21 días | **21,38 Bs** | 58,02 Bs | **63,2%** |

Acierta mejor que “no cambiar nada” en el **85%** de los días.

> Los números son honestos: a 21 días el error típico es ~2,4% del valor. Ningún
> modelo serio puede prometer más que eso en un mercado con saltos de régimen.

---

## Sincronización con R2 (opcional)

Si están las variables `R2_*` en `.env`, el autopilot sube `state.json`,
`series.csv`, `metrics.json`, `forecast.json` y `forecasts_log.csv`.

- Sube **solo cuando hay dato nuevo o cambio**; si no, un heartbeat cada 30 minutos.
- La descarga es **atómica** y solo reemplaza el estado local si el remoto es más
  nuevo (compara `last_date` / `last_poll`).
- Sin credenciales, todo queda local y no hay errores.

## Dashboard (GitHub Pages)

`index.html` lee `forecast.json` y `series.csv` desde la URL pública de R2. Esa URL
no se versiona: el workflow `.github/workflows/pages.yml` genera `config.js` desde
el secreto `R2_PUBLIC_URL`. En local, `autopilot.py` genera `config.js` a partir de
`.env`; también se puede pasar `?r2=https://...` o guardarla en el navegador.
`.gitignore` excluye `config.js`.

---

## Archivos (todo se reemplaza, nada se acumula)

| Archivo | Contenido | Política |
|---|---|---|
| `state/series.csv` | 1 fila por día hábil | reemplazo atómico |
| `state/state.json` | pesos, errores, pendientes, log | reemplazo atómico |
| `state/forecast.json` | pronóstico vigente 1d/5d/21d + camino de 21 días | reemplazo atómico |
| `state/metrics.json` | MAE/MAPE/aciertos por horizonte | reemplazo atómico |
| `state/forecasts_log.csv` | pronósticos ya puntuados | reemplazo atómico |
| `state/events.log` | últimas 500 líneas | se recorta |
| `state/autopilot.pid` | candado de instancia | se borra al salir |

La serie crece ~250 filas por año (una por día hábil). No hay un archivo por hora.

---

## Salida actual (ejemplo)

```
+ 1 día  (2026-10-21): 882,866179  ->  882,87   [80%: 878,75–886,68]
+ 5 días (2026-10-28): 889,657585  ->  889,66   [80%: 875,54–905,73]
+21 días (2026-11-19): 913,799575  ->  913,80   [80%: 839,66–1.003,20]
```

Se muestran **sin redondear** (6 decimales) y **redondeados** (2 decimales).
Los cruces EUR/CNY/TRY/RUB se proyectan con la cotización internacional del día
(supuesta constante); sirven de referencia, no de compromiso.

---

## Detección de la publicación

No se asume una hora: el sistema sondea y compara la **Fecha Valor**. Medición
sobre 554 hojas oficiales:

- mediana: **16:20**
- percentil 10: 15:02 · percentil 90: 17:58
- mínimo 14:28 · máximo 22:44

Por eso sondea cada 3 minutos entre 13:00 y 21:00, y cada 30 minutos el resto.
Cada día guarda la hora real detectada en `pub_times` para afinar la ventana.

---

## Limitaciones

- **Los saltos no se predicen.** El modelo acierta el paso normal; un ajuste de
  +0,5% a +3% se sale de cualquier banda.
- **El mes es un escenario**, no una promesa. La banda del 80% es amplia a propósito.
- **Los cruces dependen del mercado internacional**, que es un random walk.
- **Feriados**: se leen de `data/holidays.txt`; hay que actualizarlo cada año.
- Si el BCV cambia la metodología (nuevo convenio, banda, intervención), hay que
  revisar el modelo con las herramientas de análisis (carpeta `bcv-research/`).
