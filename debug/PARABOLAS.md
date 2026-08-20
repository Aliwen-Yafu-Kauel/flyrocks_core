# La parábola en el aire no es una parábola en la imagen

> **Qué documenta esto:** por qué una roca que vuela en parábola **no** se dibuja
> como parábola en el video del dron, qué le rompe eso al pipeline, y para qué
> sirve `demo/trayectorias.html` — el visor chico donde las pintamos a mano.
> Primera versión: 2026-08-06.
>
> Relacionados: `debug/PENDIENTES.md` §P5 (reconstrucción de las azules),
> `debug/ESTADO_Y_PENDIENTES.md` §5–§6, `debug/homografia.py`.

---

## 1. El problema en una línea

La roca describe una parábola **en el aire**. Lo que grabamos es su **proyección**
en el sensor del dron, y esa proyección no es una parábola: es una curva racional
que puede **frenarse y devolverse** en la imagen aunque la roca nunca retroceda.

Todo lo que en el pipeline asume "trayectoria = línea recta radial" o
"trayectoria = arco parabólico" está midiendo la forma equivocada.

---

## 2. Por qué. La geometría, sin misterio

Cámara pinhole cenital a altura `h` sobre el suelo. Una roca sale a distancia
horizontal `r0` del **nadir** (el punto justo debajo del dron), con velocidad
horizontal `v` y velocidad vertical inicial `u`:

```
distancia horizontal real:   r(t) = r0 + v·t          ← recta, sin arrastre
altura:                      z(t) = u·t − ½·g·t²      ← parábola
```

La proyección en la imagen **no** divide por `h`, divide por la distancia de la
roca **a la cámara**, que es `h − z(t)`:

```
                    r0 + v·t
   r_img(t)  ∝   ──────────────
                  h − u·t + ½g·t²
```

Ahí está todo el asunto: el denominador **también depende del tiempo**. Mientras
la roca sube, se acerca a la cámara y su imagen se agranda; mientras cae, se
aleja y se encoge. Ese efecto de zoom se **suma** al avance horizontal.

Consecuencias directas:

- **Una recta radial en el suelo no se ve recta en la imagen** salvo que
  `r0 = 0`. Con `r0 = 0` la traza sí es radial, pero **no** avanza a velocidad
  constante: se estira al subir y se comprime al caer.
- **La imagen de la parábola no es una parábola.** Es una función racional del
  tiempo. En términos proyectivos: la trayectoria vive en un plano vertical, y la
  imagen de una cónica bajo una proyectividad es otra cónica, pero **el tipo no
  se conserva** — la parábola se ve como arco de elipse o de hipérbola. Solo si
  la proyección es aproximadamente **afín** (dron mucho más alto que el vuelo de
  la roca) se conserva la forma parabólica. El "mucho más alto" tiene número
  exacto, y está en la sección siguiente.

### Lo que además ensucia (no cuantificado aquí)

| Efecto | Por qué importa |
|---|---|
| **Arrastre del aire** | La trayectoria real **ni siquiera es parábola en 3D**: la rama de caída es más empinada que la de subida. Una roca chica a alta velocidad no es un proyectil ideal. |
| **Deriva residual del dron** | Medido: frame 452 vs 38 = **18.5 px** de corrimiento (`PENDIENTES.md` §P9). Aparece como curvatura falsa en trazas largas. |
| **Eje óptico no exactamente cenital** | Rompe la simetría radial: el nadir no está en el centro del cuadro. |

---

## 3. El punto de retroceso, con fórmula cerrada

Derivando `r_img(t)` para el caso radial puro (`r0 = 0`), la velocidad aparente
se anula en:

```
   t* = √(2h / g)        ← independiente de la fuerza con que salió la roca
```

Antes de `t*` la traza se aleja del nadir; después **se devuelve hacia el
centro**. Eso ocurre *dentro del vuelo* (`T = 2u/g`) solo si:

```
   √(2h/g) < 2u/g   ⟺   h < 4 · z_max          (z_max = u²/2g)
```

**Es exactamente el criterio que salió de las 42 simulaciones numéricas**
registradas en `PENDIENTES.md` §P5 ("si `altura_dron < 4 × altura_máxima`, la
traza se devuelve"). Que la derivación analítica lo reproduzca clavado sube
mucho la confianza en la hipótesis.

Con el dron a ~250 m (altura deducida de la escala de la homografía, **aún sin
medir en terreno**):

| `z_max` de la roca | vel. vertical | vuelo | ¿se devuelve? |
|---|---|---|---|
| 20 m | 19.8 m/s | 4.0 s | no |
| 40 m | 28.0 m/s | 5.7 s | no |
| **62.5 m** | 35.0 m/s | 7.1 s | **límite exacto** |
| 100 m | 44.3 m/s | 9.0 s | **sí**, al 79 % del vuelo |
| 150 m | 54.2 m/s | 11.1 s | **sí**, al 65 % del vuelo |

### Matiz importante (refina lo escrito en §P5)

Con `r0 ≠ 0` la condición de retroceso pasa a ser
`v·(h − ½g·t²) + r0·(u − g·t) = 0`. De ahí:

- **Roca lejos del nadir, o de vuelo poco radial** (`r0/v` grande): el retroceso
  tiende al **ápice** (`t = u/g`) y ocurre **siempre**, sin importar la altura
  del dron. Es puro efecto de zoom.
- **Roca radial desde el nadir** (`r0 → 0`): es el caso **más difícil** de que se
  devuelva, y ahí manda `t* = √(2h/g)`.

O sea: `h < 4·z_max` es la condición para que se devuelvan **todas**, incluida la
peor. Fuera de ese régimen, las lejanas al nadir se devuelven igual.

> ⚠️ Esto es derivación en el modelo ideal (sin arrastre, cámara perfectamente
> cenital). Sirve para entender el mecanismo y para diseñar el filtro; **no
> reemplaza** verificar los dos pedazos de azul#1 contra `polar_result.npz`, que
> sigue pendiente en §P5.

---

## 4. Qué rompe esto en el pipeline

1. **El tracker parte las trayectorias en dos.** `polar_v2` filtra por
   continuidad física **sin reversas**. Si la traza real retrocede en el punto
   `t*`, el filtro la corta justo ahí → **exactamente 2 pedazos**, que es lo que
   el usuario describe para azul#1 ("el que va y el que vuelve").
2. **Los filtros de forma botan lo que más importa.** Linealidad y ajuste
   parabólico premian trazas rectas y cortas. Las que se curvan raro son
   justamente las que suben más… es decir, **las más peligrosas**.
3. **El sesgo va en la dirección mala.** Mientras más alto vuela una roca, peor
   la modelamos y más probable es que la descartemos. Este es el punto que hay
   que decirle al cliente sin adornos.

**Corolario de diseño:** la reconstrucción correcta no es un dedup más listo —
es **permitir la reversa** cuando la geometría predice que debe ocurrir.

---

## 5. Por eso las pintamos a mano: `demo/trayectorias.html`

Mientras el automático no modele esto, necesitamos una vía para **poner la
trayectoria correcta encima de la máscara** y trabajar con ella. Eso es el
visor: un solo archivo HTML, sin dependencias, se abre con doble clic.

Se le arrastra la máscara de intensidad (`out/6_mascaras/1_intensidad.png`),
se marca **clic en el inicio → clic en el fin**, y se arrastra el manejador
hasta que la curva calce con la estela.

### Las decisiones de diseño responden 1 a 1 a la geometría de arriba

| Decisión | Por qué |
|---|---|
| **Bézier cuadrática** por defecto | Una Bézier cuadrática **es** un arco de parábola, exacto. Es el modelo correcto cuando la aproximación afín aguanta (roca baja, dron alto). |
| **Tecla `C` → cúbica** | Da ángulos de entrada y salida **independientes**. Es lo que se necesita cuando la forma **no** es parabólica: asimetría por arrastre, y el arqueo por paralaje. La elevación de grado es **exacta** (misma curva, píxel por píxel), así que pasar de Q a C y volver no deforma nada. |
| **Tecla `I` → invertir** | El sentido de vuelo es un **dato físico**. Invierte la trayectoria completa (`p0↔p1`, `c1↔c2`), no solo la flecha, para que el JSON no exporte como "inicio" lo que era el final. |
| **Puntos siempre en coords. de imagen** | Nada se guarda en píxeles de pantalla. El zoom no contamina el dato y la exportación sale a resolución **nativa 3840×2160**. |
| **Halo aparte del trazo** | Es realce de edición: va *debajo* del trazo y **no** entra a la imagen exportada. El lienzo es vista fiel de lo que se va a entregar. |

### Lo que entrega

- **`…_trayectorias.png`** — máscara + trazos, a resolución nativa. Para la
  demo y para el informe.
- **`…_trayectorias.json`** — las trayectorias como **datos**: puntos de control,
  `largo_cuerda_px`, `angulo_grados`, `curvatura_px`. Mismo patrón que
  `h_matrix.json` / `capas_params.json`.

El JSON es el aporte real. Es un input **mucho mejor que el PNG pintado a dedo**
para §P5: la parábola llega con su geometría explícita y su dirección, sin que el
algoritmo tenga que adivinar la intención desde un trazo grueso. Y sirve como
**ground truth** para contrastar la curvatura medida a mano contra la que predice
el modelo de §3.

### Atajos

`clic`→`clic` trazar · `C` segundo manejador · `I` invertir sentido ·
`Supr` borrar · `Ctrl+Z` deshacer · `rueda` zoom · `espacio`/`botón derecho` paneo ·
`F` encuadrar · `Esc` deseleccionar

---

## 6. Lo que esta herramienta NO hace (leer antes de sacar números)

- **`curvatura_px` y `angulo_grados` son de imagen, no de mundo.** Por todo lo
  de §2, dos trazas con la misma curvatura en píxeles en zonas distintas del
  cuadro **no** corresponden a la misma trayectoria física. No promediarlas.
- **La homografía no convierte el arco a metros.** `h_matrix` (`homografia.py`)
  es un ajuste mundo→píxel anclado a los **tiros**, que están en el suelo: vale
  en `z = 0`. Los puntos intermedios del arco están **en el aire**, y aplicarles
  `H⁻¹` los manda a una posición equivocada. **Solo el origen y el punto de
  impacto** son convertibles a metros — que, para "distancia de alcance del
  flyrock", por suerte es justo lo que se necesita.
- **Es trazado manual.** Es mecanismo de rescate y referencia de validación, no
  medición automática. La misma lógica del azul en la pintura
  (`ESTADO_Y_PENDIENTES.md` §3).

---

## 7. Qué falta

1. **Medir la altura real del dron.** Hoy está deducida de la escala de la
   homografía (8.58 px/m) más un FOV típico. Todo §3 depende de ese número.
2. **Verificar los 2 pedazos de azul#1** contra `polar_result.npz`: ¿se tocan
   cerca del `t*` previsto? Si sí, la hipótesis queda cerrada.
3. **Permitir la reversa en el tracker** cuando la geometría la predice, en vez
   de prohibirla siempre (`polar_v2`).
4. **Rehacer la simulación con `scipy`** — el ajuste ingenuo con Powell da
   resultados falsos; hay que ajustar la Bézier por alternancia con
   parametrización monótona.
