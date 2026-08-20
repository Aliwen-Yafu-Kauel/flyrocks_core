# Modelos

Aquí van los pesos del filtro de humo por IA. **No se versionan**: son binarios
grandes que cambian por su cuenta, y `.gitignore` los excluye (`*.onnx`).

    modelos/detovision_model_v18.onnx

De dónde sale: lo produce el equipo de IA (Aliwen). Si falta, el nodo
`1.5_AISmokeFilter` no puede cargar la sesión ONNX y **el análisis completo
falla**, así que este archivo es requisito para correr el pipeline.

La ruta se puede cambiar sin tocar código, con la variable de entorno
`MODELO_ONNX` (ver `entrega/docker-compose.yml`). El valor por defecto es
`modelos/detovision_model_v18.onnx`, relativo a `/app` dentro del contenedor.

Esta carpeta viaja al cliente dentro del paquete: `armar_paquete.py` copia el
core entero salvo lo que excluye a propósito, y los modelos no están excluidos.
