# Servicio local de traducción — NLLB-200-3.3B

Microservicio HTTP de traducción **inglés → español (LATAM)** que corre entero en
esta máquina. Motor: NLLB-200-3.3B sobre CTranslate2 en int8, en la RTX 3060.

Después de la descarga inicial del modelo **no hay ninguna llamada de red**: los
pesos están en `D:\ollama\models\`, la inferencia es local y el tokenizer se
carga con `local_files_only=True`. Puedes desconectar el cable y sigue
traduciendo.

**Por qué NLLB y no un LLM instruccional:** es encoder-decoder dedicado a
traducción. No tiene system prompt, no puede negarse, comentar ni agregar
preámbulos. Entra texto, sale texto traducido. Eso elimina toda la capa de
validación de salida que hace falta con un modelo de instrucciones.

---

## Estructura

```
services/translator/
├── setup.ps1               instalación completa (venv, deps, CUDA, modelo)
├── run.ps1                 arranca uvicorn con UN worker
├── requirements.txt        versiones exactas, cada pin con su motivo
├── app/
│   ├── main.py             FastAPI: los 3 endpoints y el lifespan
│   ├── engine.py           CTranslate2 + tokenizer NLLB (se carga una vez)
│   ├── segmenter.py        segmentación por oración y reensamblado exacto
│   ├── postedit.py         ES peninsular -> ES neutro LATAM
│   ├── languages.py        FLORES-200 + puente ISO-639
│   ├── cudaload.py         las DLL de CUDA en Windows (lee su docstring)
│   ├── gpu.py              VRAM vía NVML
│   ├── config.py           todo configurable por variable de entorno
│   ├── schemas.py          request/response
│   └── errors.py           errores con su código HTTP
├── data/
│   └── postedit_spa_Latn.txt   la lista de post-edición, editable
├── scripts/
│   ├── verify_cuda.py      ¿CTranslate2 ve la GPU? y si no, por qué
│   └── download_model.py   descarga el modelo ya convertido
└── tests/
    └── test_logic.py       56 pruebas sin GPU, sin modelo, sin dependencias
```

---

## Instalación

```bash
cd services/translator
```

```powershell
.\setup.ps1
```

Hace, en este orden: crea `.venv` (separado del venv de la app), instala las
dependencias fijadas, resuelve las DLL de CUDA, **comprueba que CTranslate2 vea
la GPU**, y sólo entonces descarga los 3.5 GB del modelo.

Ese orden es a propósito. Enterarte de que CUDA no funciona *después* de esperar
diez minutos de descarga es la peor secuencia posible.

### Si el paso 5 falla

Es el punto de fricción de esta plataforma. CTranslate2 necesita cuBLAS y cuDNN
al cargar; en Linux los paquetes pip `nvidia-*` lo resuelven, en Windows no hay
nada que lo haga por ti, y desde Python 3.8 el intérprete ya no busca en PATH las
dependencias de módulos de extensión. Si no las encuentra, `import ctranslate2`
**funciona** y `get_cuda_device_count()` contesta 0 — que se lee igual que "no
hay GPU".

```powershell
.\setup.ps1 -WithTorchCuda
```

Instala el build CUDA de PyTorch (~2.5 GB) *sólo* para tomar prestadas sus DLL de
`torch/lib`. El servicio nunca importa torch: `cudaload.py` averigua la ruta con
`importlib.util.find_spec`, sin pagar el import. Por eso torch no está en
`requirements.txt` y hay que pedirlo con la bandera — no se instala a tus
espaldas.

Para diagnosticar por separado:

```powershell
.\.venv\Scripts\python.exe scripts\verify_cuda.py
```

---

## Arrancar

```powershell
.\run.ps1
```

- `http://127.0.0.1:8100` — sólo loopback, el servicio no tiene autenticación
- `http://127.0.0.1:8100/docs` — OpenAPI interactivo
- Primer arranque: ~20 s (3.5 GB de pesos + una oración de warm-up)

**Un worker, siempre.** Cada worker de uvicorn es un proceso aparte que cargaría
su propia copia del modelo: `--workers 2` en esta tarjeta son 7.2 GB de pesos y
un OOM inmediato. `run.ps1` lo pasa explícito para que no dependa del default.

---

## Endpoints

### `POST /translate`

```bash
curl -s -X POST http://127.0.0.1:8100/translate \
  -H "Content-Type: application/json" \
  -d '{"text": "A class is a blueprint. It defines the data and behaviour of its instances."}'
```

```json
{
  "text": "Una clase es un plano. Define los datos y el comportamiento de sus instancias.",
  "source_lang": "eng_Latn",
  "target_lang": "spa_Latn",
  "sentences": 2,
  "passed_through": 0,
  "beam_size": 4,
  "postedit_applied": true,
  "elapsed_ms": 412
}
```

Todos los campos menos `text` son opcionales:

| campo | default | notas |
|---|---|---|
| `source_lang` | `eng_Latn` | FLORES-200 o ISO (`en`, `es`, `es-LATAM`) |
| `target_lang` | `spa_Latn` | código no soportado → 400 con la lista válida |
| `beam_size` | 4 | 1 a 8. `1` es ~3x más rápido y algo peor |
| `postedit` | `true` para `spa_Latn` | `false` desactiva la lista LATAM |

### `POST /translate/batch`

```bash
curl -s -X POST http://127.0.0.1:8100/translate/batch \
  -H "Content-Type: application/json" \
  -d '{"texts": ["The first paragraph.", "", "The third one, with two sentences. Here is the second."]}'
```

Devuelve `translations` **alineado por índice** con la entrada. Los elementos
vacíos regresan vacíos: un capítulo tiene párrafos en blanco y el llamador no
tiene por qué filtrarlos.

Lo importante de este endpoint no es el ahorro de round-trips HTTP: es que las
oraciones de **todos** los textos se juntan en una sola lista y se agrupan en
llamadas grandes a la GPU. 400 párrafos cortos se vuelven un puñado de batches,
no 400 llamadas.

### `GET /health`

```bash
curl -s http://127.0.0.1:8100/health
```

`status` es `ok`, `degraded` (poca VRAM libre) o `error` (modelo no cargado, con
503 y el motivo en `detail`).

En PowerShell, para JSON con comillas es menos doloroso así:

```powershell
Invoke-RestMethod -Uri http://127.0.0.1:8100/translate -Method Post -ContentType 'application/json' -Body (@{ text = 'A class is a blueprint.' } | ConvertTo-Json)
```

---

## VRAM: qué batch es seguro

Medido en esta máquina:

| | MiB |
|---|---|
| Total de la 3060 | 12288 |
| Escritorio de Windows en reposo (explorer, Chrome, overlay NVIDIA, trays) | 2200 – 2600 |
| Huella del modelo int8 + contexto CUDA | ~3600 |
| **Margen de trabajo** | **~6000** |

Los batches se miden en **tokens, no en oraciones**
(`batch_type="tokens"`). 32 oraciones largas y 32 cortas son cargas
completamente distintas; un contador fijo de oraciones es una bomba de tiempo.

| `NLLB_MAX_BATCH_TOKENS` | oraciones aprox. | veredicto |
|---|---|---|
| 1024 | 30 – 40 | muy conservador |
| **2048** | **60 – 80** | **default** |
| 4096 | 120 – 160 | sirve si nada más usa la GPU |
| 8192 | 240 – 320 | demasiado cerca del límite |

El default no está maximizado a propósito: la línea base del escritorio **no es
fija**. Chrome con video te sube 1-2 GB sin avisar, y el margen es justamente
para eso.

No tengo medición propia del costo en VRAM de las activaciones por nivel de
batch — depende del largo real de tus oraciones. La forma de saberlo es empírica:
`GET /health` antes y durante un capítulo grande, y comparar `gpu.free_mb`.

### Cómo detectar que estás cerca del límite

1. **`/health`** → `status: "degraded"` y `detail` cuando quedan menos de 1536 MB
   libres (`NLLB_VRAM_WARN_FREE_MB`).
2. **El log del servicio.** Si un batch revienta por OOM, el servicio parte el
   presupuesto a la mitad y reintenta una vez, dejando un `WARNING`. Ese warning
   es la señal temprana: funciona, pero vas al límite.
3. **Al arrancar.** Si hay menos de 4608 MB libres (`NLLB_VRAM_MIN_FREE_MB`) el
   servicio se niega a cargar con un mensaje que dice cuánto hay y cuánto falta,
   en vez de un error de CUDA ilegible 20 segundos después.

Palancas cuando se aprieta, en orden de menor daño:

```
NLLB_MAX_BATCH_TOKENS=1024    # primero esto
NLLB_BEAM_SIZE=1              # ~3x más rápido, 1/4 de memoria de decoding, algo peor
NLLB_COMPUTE_TYPE=int8_float32 # si int8_float16 no está soportado
```

**Aviso de VRAM compartida:** cualquier otro modelo cargado en esta GPU se queda
con su parte hasta que lo descargues. Un modelo de 6.6 GB más NLLB suman 12.4 GB
de los 12 que hay, y no cabe. `/health` te dice cuánta VRAM queda libre antes de
que eso se convierta en un OOM.

---

## Docker: fuera, y por qué

En Windows el paso de GPU exige WSL2 + Docker Desktop con backend WSL2 +
nvidia-container-toolkit dentro de WSL. Funciona, pero:

- los 3.5 GB del modelo hay que montarlos o hornearlos en una imagen enorme;
- la VRAM se comparte con el escritorio a través del shim de WSL, así que el
  presupuesto de arriba deja de ser observable;
- le agregas una capa justo al problema más probable, las DLL de CUDA.

Para un servicio local de un solo proceso, sin historia de despliegue, estorba
más de lo que ayuda. Si algún día esto se va a un servidor Linux con GPU, ahí sí
vale la pena y es trivial.

---

## Pruebas

```bash
python tests/test_logic.py
```

56 pruebas sin GPU, sin modelo y sin dependencias instaladas: corren con el
Python del sistema antes de `setup.ps1`. Cubren lo que es fácil equivocar en
silencio — el round trip de segmentación, el orden en que vuelven las
traducciones, y las reglas de post-edición donde un reemplazo naíf deja español
roto.

---

## Cosas que pueden fallar (y que ya están cubiertas)

**`max_decoding_length` de CTranslate2 es 256 tokens por default.** El español
corre 15-25% más largo que el inglés, así que una oración fuente larga volvería
cortada a media frase, sin error en ningún lado. Está subido a 1024.

**El token de idioma fuente cambió de lugar entre versiones de transformers**
(la bandera `legacy_behaviour`). En la posición equivocada no hay excepción: sólo
traducciones peores, que es el bug más difícil de notar. `engine.py` verifica la
posición al arrancar y falla ruidosamente.

**NLLB tiene un solo español.** No existe `es-419` en FLORES-200 y no hay prompt
donde pedir "neutro LATAM". Eso lo cubre `data/postedit_spa_Latn.txt`, de forma
determinista. Las reglas se aplican **en orden de archivo**, que es lo que hace
tratable el cambio de género: `el ordenador → la computadora` tiene que dispararse
antes que `ordenador → computadora`, o queda `el computadora`. El archivo trae una
sección de reglas riesgosas comentadas, cada una con su motivo (por ejemplo
`os => les` está fuera porque `\bos\b` sin distinguir mayúsculas también casa con
`OS`, y convertiría "the OS" en "the LES").

**Una "oración" puede pasarse de 512 tokens** — una tabla aplanada, una línea
corrida de un PDF mal convertido. Se corta primero por cláusulas y, si no hay
dónde, por ventanas de palabras. Un "token" solo demasiado largo (una URL, un
blob base64) **no se manda al modelo**: viaja intacto, porque partirlo lo
corrompería y traducirlo no tiene sentido.

**`pysbd.Segmenter` guarda estado por instancia**, así que una sola instancia
compartida entre hilos corrompe resultados. Se usa una por hilo.

**El timeout contesta al cliente pero no cancela la GPU.** `translate_batch` es
código C++ bloqueante; `asyncio.wait_for` te responde, pero ese trabajo sigue
hasta terminar y sigue sosteniendo el lock del engine. Lo que de verdad acota el
trabajo es `NLLB_MAX_TEXT_CHARS`. No está disfrazado de cancelación.

**En Windows con WDDM, NVML no puede atribuir VRAM por proceso**
(`nvidia-smi --query-compute-apps` reporta `[N/A]` para todos). `/health` da el
agregado: suficiente para saber que estás cerca del límite, insuficiente para
señalar al culpable. El propio payload lo dice en `gpu.note`.

---

## Integración con oreilly-ingest — conectada

`plugins/translator.py` habla con este servicio. `config.py` de la app trae
`TRANSLATOR_URL` y el mapeo `es-LATAM → spa_Latn`. El dropdown **Translate** de
la modal de descarga ya lo usa.

### Lo que el plugin hace y por qué

El modelo no acepta instrucciones, así que los tags se sacan de su camino y se
devuelven después. Cada bloque hoja se convierte en una plantilla donde cada
pieza de markup es un placeholder numérico:

```
<p>The <code>pd.Series</code> class is a <i>blueprint</i>.</p>
  se codifica como
"The %%0%% class is a %%1%%blueprint%%2%%."
```

Tres decisiones que salieron de **medir contra este servicio**, no de suponer:

1. **Placeholders opacos, no nombres de tag reales.** Probé las dos formas.
   Con `<i>...</i>`, dado *"Choose &lt;i&gt;one&lt;/i&gt;, &lt;i&gt;two&lt;/i&gt;
   or &lt;i&gt;three&lt;/i&gt;."*, el modelo devolvió *"Elige uno, dos o tres."*
   — impecable, y sin un solo tag. **Reconoce** el markup y se siente libre de
   tirarlo. `%%0%%` es basura sin significado que simplemente copia.
   Marcador de resultado: opacos 6/9, tags reales 5/9.
2. **El salto de línea del HTML fuente costaba párrafos completos, y ese era mi
   bug, no del modelo.** La plantilla llegaba con saltos a mitad de oración
   (el HTML del editor envuelve sus líneas); un placeholder que quedaba solo al
   inicio de una línea envuelta desaparecía, y el bloque perdía su traducción.
   Colapsar los espacios antes de enviar lo arregló: era la causa principal.
   La pérdida sigue siendo real pero es poco frecuente en markup real — una
   muestra de 13 bloques con 18 placeholders perdió un par de formato y ningún
   contenido protegido. No depende de cuántos haya (sobrevivieron 12 donde
   fallaron 6), y cambiar el beam no rescata nada (0 de 4).
3. **Por eso las pérdidas se clasifican en vez de ser fatales.** Perder el
   placeholder de un `<code>` borraría contenido, así que ese bloque se rechaza y
   se reintenta con el formato inline aplanado (muchos menos placeholders que
   perder). Perder un `<i>` sólo cuesta cursivas, así que la traducción **se
   queda** y el formato se tira. Un párrafo en español sin cursivas le gana a uno
   en inglés con ellas.

Los atributos se restauran del elemento original y nunca se envían, así que un
`href` no puede volver corrupto.

### Ideas tomadas de oomol-lab/epub-translator

Ese proyecto resuelve el mismo problema quirúrgicamente, para un modelo que sí
acepta instrucciones. Tres de sus ideas se trasladan; una no.

- **Bloque = "todo lo que no es inline"**, derivado de la lista de elementos
  inline de MDN, en vez de una lista de tags de bloque escrita a mano. Es
  exhaustiva, y arregló un bug real: un `<div>` con puro contenido inline no
  entraba en la lista vieja, así que sus nodos de texto se traducían uno por uno
  como fragmentos ciegos.
- **El markup se restaura del elemento original**, nunca de lo que devolvió el
  modelo.
- **Reensamblado best-effort en lugar de todo-o-nada.** Es la idea que más
  cambió el resultado aquí, y es de donde salió la clasificación del punto 3.
- **No tomada:** su codificación con nombres de tag reales e ids mínimos (un id
  sólo cuando dos tags del mismo nombre son indistinguibles). Depende de poder
  pedirle al modelo que preserve los tags, y la medición muestra que aquí es
  peor.

### Lo que falta

Un glosario de términos intocables. Al modelo no puedes decirle "deja los
identificadores en inglés", así que traduce *string*, *array*, *commit*. La
solución es una lista de términos que pase por el mismo mecanismo de
placeholders que el código.
