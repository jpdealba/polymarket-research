"""Glosario en espanol para orientarse en el CLI y el dashboard. Pagina
estatica, no llama a la API ni a la base de datos."""

from __future__ import annotations

import streamlit as st

st.title("Glosario")
st.caption(
    "Palabras clave del proyecto explicadas en espanol sencillo, con ejemplos. "
    "Util antes de correr comandos `pmr` o de leer las otras paginas del dashboard."
)

st.markdown(
    """
### Wallet (billetera)
Una direccion de Polymarket que estamos investigando. Todo en el sistema
se organiza alrededor de wallets: eventos, posiciones, metricas.

> Ejemplo: `0x2005d16a84ceefa912d4e380cd32e7ff827875ea` (apodada **RN1** en este proyecto).

```bash
pmr wallet add 0x... --name "RN1"
```

---

### Watchlist (lista vigilada)
El grupo de wallets que estamos sincronizando activamente ahora mismo.
No es "todas las wallets del mundo", solo las que decidimos seguir.

---

### Fill (ejecucion)
**Un solo trade ejecutado.** Es la unidad mas basica: una wallet compro o
vendio X cantidad de un token a un precio, en un momento dado.

> Ejemplo: RN1 compro 500 shares del token "Mexico gana" a $0.62 el 2026-06-14 10:03 UTC.

Un fill puede venir marcado como:
- **Maker**: su orden ya estaba puesta en el libro y alguien mas la ejecuto (dio liquidez).
- **Taker**: su orden entro y se cruzo contra una orden ya existente (tomo liquidez).

---

### Wallet Event (evento)
Es un concepto **mas amplio que un fill**. Un evento es cualquier accion
atomica de la wallet que cambia su posicion o su saldo: un TRADE (= fill),
pero tambien un MERGE, un SPLIT, un REDEEM (cobro al resolverse el mercado),
un REWARD (recompensa de Polymarket), etc.

Es decir: **todo fill es un evento, pero no todo evento es un fill.**

Estos eventos viven en el "ledger" (el libro contable), que nunca se borra
ni se edita: si algo estaba mal, se agrega un evento nuevo que lo corrige.

---

### Market (mercado)
Una pregunta cerrada de Polymarket con sus resultados posibles (tokens) y
su resolucion. Ejemplo: "Gana Mexico vs Corea?" con dos tokens: Si / No.
Se identifica por su `conditionId`.

---

### Event (evento de Polymarket, distinto de "wallet event")
**Cuidado, esta palabra se usa dos veces con significados distintos:**

- **Wallet Event**: una accion de una wallet (ver arriba).
- **Event de Polymarket**: un grupo de mercados relacionados, por ejemplo
  "Mundial 2026 - Grupo A", que agrupa los partidos Mexico/Corea,
  Mexico/Suiza, etc. Cada partido es un Market dentro de ese Event.

Cuando hablamos del "World Cup Watch", ese "World Cup" es el Event de
Polymarket que agrupa todos los partidos que estamos vigilando.

---

### Backfill
Traer **todo el historial** de una wallet desde su primera actividad hasta
hoy. Se hace una sola vez cuando agregamos una wallet nueva a la watchlist.

```bash
pmr sync backfill 0x...
```

### Incremental Sync
Traer solo la actividad **nueva** desde la ultima vez que sincronizamos.
Esto es lo que corre el scheduler cada cierto tiempo, automaticamente.

### Ingest
Convertir los datos crudos ya descargados (Raw Store) en filas del ledger
(`wallet_events`). Es el paso que produce los eventos que se pueden analizar.

```bash
pmr ingest run --wallet 0x...
```

### Enrichment (enriquecimiento)
Despues de que un fill ya existe, se le agrega informacion extra que viene
de la blockchain: si fue maker o taker, el hash de la orden, la comision (fee)
pagada. El enrichment puede tardar/quedarse atras respecto al fill original
("enrichment lag"): por eso a veces un fill aparece sin su rol maker/taker
todavia.

---

### Coverage (cobertura)
Que tan completos/confiables son nuestros datos para un conjunto de fills.
Cada fill se clasifica segun la calidad del contexto de libro de ordenes
(book) que logramos capturar cerca de ese momento:

| Estado | Significado |
|---|---|
| excellent / good | tenemos el libro de ordenes muy cerca en el tiempo del fill |
| usable | tenemos el libro, pero algo mas lejano en el tiempo |
| weak | el contexto es debil, usar con precaucion |
| stale | el dato de libro que tenemos esta viejo |
| missing | no logramos capturar contexto de libro para ese fill |

**Strict** = solo cuenta excellent + good (el estandar mas exigente).
**Loose (+usable)** = tambien cuenta usable (mas permisivo, todavia utilizable).
"""
)

st.divider()

st.subheader('Por que "All Fills Coverage" muestra un numero distinto al total de fills de la wallet')

st.markdown(
    """
En la pagina **World Cup Watch** viste algo asi:

> RN1 — All Fills: **6126**, All Stale/Missing: 590

Y quizas esperabas ver millones, porque RN1 tiene millones de eventos en su
historial completo (todos los mercados que ha tocado, no solo el Mundial).

La razon: **"All Fills Coverage" no cuenta todos los fills de la wallet.**
Cuenta unicamente los fills que cumplen *dos* condiciones a la vez:

1. Son fills en un **token que esta en la watchlist activa del Mundial**
   (`is_active = 1` en `watchlist_tokens`) — es decir, solo partidos/mercados
   que decidimos vigilar para este analisis.
2. Ya tienen una fila calculada en `all_fill_context` — la tabla que junta
   cada fill con el estado del libro de ordenes (bid/ask) de ese momento.
   Esta tabla se llena con el boton **"Refresh fills & coverage"**.

Entonces "All Fills = 6126" quiere decir: *de todos los millones de eventos
historicos de RN1, estos son los que corresponden a mercados del Mundial que
estamos vigilando ahora mismo, y para los que ya calculamos contexto de libro.*

Es una vista deliberadamente **acotada** (scoped), no un conteo global. Si
agregamos mas partidos a la watchlist, o corremos el refresh de nuevo, este
numero puede subir; nunca va a acercarse al total historico de la wallet
porque ese total incluye mercados que no tienen nada que ver con el Mundial.
"""
)
