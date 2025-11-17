# --- Etapa 1: Imagen base ---
# Usamos una imagen slim oficial de Python
FROM python:3.11-slim

# Establece el directorio de trabajo dentro del contenedor
WORKDIR /app

# Establece variables de entorno para Python
ENV PYTHONDONTWRITEBYTECODE 1
ENV PYTHONUNBUFFERED 1
ENV PORT 5005

# Instalar curl para que los cron jobs de Dokploy (bash -c 'curl ...') funcionen.
# Se debe ejecutar como root, antes de cambiar a 'USER app'.
RUN apt-get update && apt-get install -y curl --no-install-recommends \
    && rm -rf /var/lib/apt/lists/*

# --- Etapa 2: Instalación de dependencias ---
# Copia solo los archivos de requisitos primero
COPY requirements.txt .

# Instala las dependencias
# --no-cache-dir reduce el tamaño de la imagen
RUN pip install --no-cache-dir -r requirements.txt

# --- Etapa 3: Copia del código fuente ---
# Copia el resto de tu aplicación
COPY ./app /app/app

# --- Etapa 4: Configuración de usuario y ejecución ---
# (Opcional pero recomendado por seguridad)
# Crea un grupo y usuario no-root para ejecutar la app
RUN addgroup --system app && adduser --system --group app
USER app

# Expone el puerto que Gunicorn usará
EXPOSE 5005

# --- Comando de inicio ---
# Usa 'sh -c' para que las variables de entorno ($PORT) se expandan
# Este es el comando que nos diste
CMD ["sh", "-c", "gunicorn -k uvicorn.workers.UvicornWorker app.main:app -b 0.0.0.0:${PORT:-5005}"]