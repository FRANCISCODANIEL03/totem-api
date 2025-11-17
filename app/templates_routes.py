import uuid
from io import BytesIO
from PIL import Image
import boto3
import botocore
from botocore.config import Config as BotocoreConfig
from fastapi import APIRouter, UploadFile, File, Depends, HTTPException, BackgroundTasks
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session
from app.utils import get_current_user, process_with_gemini
from app.db import get_db, SessionLocal
from app import models
from app.config import (
    S3_BUCKET_NAME,
    S3_REGION,
    S3_ACCESS_KEY,
    S3_SECRET_KEY,
    S3_ENDPOINT,
    S3_USE_SSL,
    URL_PRODUCTION
)

router = APIRouter(prefix="/templates", tags=["templates"])

# ============================================================
# 🔧 CONFIGURAR CLIENTE S3 (idéntico al test que sí funcionó)
# ============================================================

protocol = "https" if S3_USE_SSL else "http"

if S3_ENDPOINT.startswith("http"):
    endpoint_url = S3_ENDPOINT
else:
    endpoint_url = f"{protocol}://{S3_ENDPOINT}"

config = BotocoreConfig(
    region_name=S3_REGION,
    signature_version="s3v4",
    s3={"addressing_style": "path"},
    retries={"max_attempts": 3, "mode": "adaptive"},
)

s3 = boto3.client(
    "s3",
    endpoint_url=endpoint_url,
    aws_access_key_id=S3_ACCESS_KEY,
    aws_secret_access_key=S3_SECRET_KEY,
    config=config,
    verify=S3_USE_SSL
)

# ==============================
# 1️⃣ SUBIR TEMPLATE
# ==============================
@router.post("/upload")
async def upload_template(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user)
):
    if file.content_type not in ["image/png", "image/jpeg", "image/jpg", "image/webp", "image/heic"]:
        raise HTTPException(status_code=400, detail="Invalid image type")

    contents = await file.read()

    # Generar UUID y clave S3
    uid = str(uuid.uuid4())
    s3_key = f"{current_user.id}/{uid}.png"

    # Guardar en la base de datos
    template = models.Template(id=uid, user_id=current_user.id, s3_key=s3_key)
    db.add(template)
    db.commit()
    db.refresh(template)

    background_tasks.add_task(process_and_upload_template, contents, s3_key, current_user.id)
    return {"uuid": uid}


# ==============================
# 2️⃣ LISTAR TEMPLATES
# ==============================
@router.get("/my")
def list_templates(db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    templates = db.query(models.Template).filter_by(user_id=current_user.id).all()
    result = []
    for t in templates:
        proxy_url = f"{URL_PRODUCTION}/templates/image/{current_user.id}/{t.id}.png"
        result.append({
            "uuid": t.id,
            "s3_key": t.s3_key,
            "url": proxy_url
        })
    return result

@router.get("/my-with-images")
def list_templates_with_images(db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    images = db.query(models.TemplateWithImage).filter_by(user_id=current_user.id).all()
    result = []
    for img in images:
        proxy_url = f"{URL_PRODUCTION}/templates/image/{current_user.id}/{img.id}.png"
        result.append({
            "uuid": img.id,
            "url": proxy_url
        })
    return result


# ==============================
# 3️⃣ INTEGRAR PERSONA EN TEMPLATE
# ==============================
@router.post("/integrate/{template_id}")
async def integrate_person(
    template_id: str,
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user)
):
    # Verificar que la plantilla exista
    template = db.query(models.Template).filter_by(id=template_id, user_id=current_user.id).first()
    if not template:
        raise HTTPException(status_code=404, detail="Template not found")

    # Generar nuevo UUID para la imagen resultante
    new_uid = str(uuid.uuid4())
    s3_key = f"{current_user.id}/{new_uid}.png"

    # Guardar el registro de TemplateWithImage
    template_with_image = models.TemplateWithImage(
        id=new_uid,
        user_id=current_user.id,
        s3_key=s3_key,
        template_id=template.id
    )
    db.add(template_with_image)
    db.commit()
    db.refresh(template_with_image)

    # Leer el archivo antes de liberar la conexión
    contents = await file.read()

    # Lanzar tarea en background
    background_tasks.add_task(process_and_integrate_person, template.s3_key, contents, s3_key)

    # Responder rápido
    return {"uuid": new_uid, "status": "processing"}

@router.post("/admin/cleanup", status_code=202)
def trigger_s3_cleanup(
    background_tasks: BackgroundTasks,
    current_user=Depends(get_current_user) # Protegido por login
):
    """
    Inicia una tarea en segundo plano para verificar todos los objetos S3
    y eliminar los registros de la BD que no tengan un archivo correspondiente.
    """
    print(f"Cleanup task triggered by user: {current_user.email}")
    background_tasks.add_task(perform_s3_cleanup)
    return {"status": "success", "message": "S3 cleanup task initiated in background."}

@router.post("/admin/internal-cleanup", status_code=202, include_in_schema=False)
def trigger_internal_cleanup(background_tasks: BackgroundTasks):
    """
    Endpoint NO protegido, para ser llamado por el cron job de Dokploy.
    Es seguro porque solo es accesible desde dentro del contenedor (localhost).
    """
    print(f"Internal cleanup task triggered by Cron Job.")
    background_tasks.add_task(perform_s3_cleanup)
    return {"status": "success", "message": "S3 internal cleanup task initiated."}

@router.get("/image/{user_id}/{filename}")
def get_template_image(user_id: str, filename: str):
    """Devuelve la imagen desde S3 a través del backend."""
    s3_key = f"{user_id}/{filename}"

    buffer = BytesIO()
    try:
        s3.download_fileobj(S3_BUCKET_NAME, s3_key, buffer)
    except Exception as e:
        raise HTTPException(status_code=404, detail=f"Image not found: {str(e)}")

    buffer.seek(0)
    return StreamingResponse(buffer, media_type="image/png")


def process_and_upload_template(contents: bytes, s3_key: str, user_id: str):
    """Esta función se ejecuta en segundo plano y procesa la imagen con Gemini y la sube a S3."""
    img = Image.open(BytesIO(contents))

    prompt = """
    Add one or more human silhouettes next to the person in the provided image.

    The silhouettes must maintain the same scale, body proportions, and relative size as the original person — not smaller, larger, thinner, or wider.

    Ensure the silhouettes blend naturally into the scene, matching the lighting, perspective, and visual style of the original image.

    The silhouettes should be clearly human-shaped but without detailed facial features (they can be semi-transparent or shaded).

    Do not modify the original person or background, only add the silhouettes or masks beside them.

    Add realistic human silhouettes next to the original person, keeping perfect scale consistency (1:1 ratio with the person height). 
    Preserve proportions and spatial coherence. Match lighting and shadow direction. 
    No distortion or unrealistic body dimensions. Mask style: smooth edges, neutral shadow tone. 
    Background untouched."""

    # Procesar con Gemini
    result_img = process_with_gemini(prompt, img)

    # Subir a S3
    buffer = BytesIO()
    result_img.save(buffer, format="PNG")
    buffer.seek(0)

    try:
        s3.put_object(
            Bucket=S3_BUCKET_NAME,
            Key=s3_key,
            Body=buffer,
            ContentType="image/png",
            ACL="public-read"
        )
    except Exception as e:
        print(f"Error uploading to S3 in background task: {str(e)}")

def process_and_integrate_person(template_s3_key: str, person_bytes: bytes, output_s3_key: str):
    """Se ejecuta en segundo plano para integrar una persona en la plantilla."""
    try:
        # Descargar la imagen base desde S3
        base_buffer = BytesIO()
        s3.download_fileobj(S3_BUCKET_NAME, template_s3_key, base_buffer)
        base_buffer.seek(0)
        base_img = Image.open(base_buffer)

        # Cargar la nueva persona
        person_img = Image.open(BytesIO(person_bytes))

        prompt = """
        Integrate the provided person photo into the image, placing them exactly where the silhouettes are located.
        Maintain perfect scale consistency and realistic proportional relation between all people — the added person must have the same relative height, body proportions, and perspective as the person in the original image.

        The inserted person should look completely human, with realistic anatomy, natural body shape, and true-to-life lighting and shadows.
        Preserve the original person’s size and position; do not alter or stylize them.
        
        Match the environmental lighting, color tone, and depth of field of the base image.
        
        Do not apply cartoon, digital painting, anime, 3D render, or illustration styles — only realistic human appearance and photographic style.

        Technical constraints / control parameters:
        Style: photorealistic, human, natural lighting
        Scale ratio: 1:1 with base person (same physical height and proportions)

        Perspective alignment: match camera angle and focal distance

        No stylization or distortion (disable art filters, stylized weights = 0)
        Keep background and base subject untouched

        Inpainting strength: 0.3–0.45 (just enough to blend seamlessly without altering the scene)
        Detail enhancement: medium realism focus, avoid over-sharpening

        Seed locking recommended for consistent proportions

        Negative prompt: 
        cartoon, painting, anime, 3d render, illustration, 
        unrealistic face, distorted body, thin limbs, exaggerated features, 
        deformed hands, out of scale, blurry."""

        # Procesar con Gemini
        result_img = process_with_gemini(prompt, base_img, other_image=person_img)

        # Guardar en memoria
        buffer = BytesIO()
        result_img.save(buffer, format="PNG")
        buffer.seek(0)

        # Subir a S3
        s3.put_object(
            Bucket=S3_BUCKET_NAME,
            Key=output_s3_key,
            Body=buffer,
            ContentType="image/png",
            ACL="public-read"
        )

        print(f"✅ Uploaded integrated image to S3: {output_s3_key}")
    except Exception as e:
        print(f"❌ Error integrating image for key {output_s3_key}: {str(e)}")


def perform_s3_cleanup():
    """
    Tarea en segundo plano para encontrar y eliminar registros huérfanos de la BD.
    Crea su propia sesión de BD para ser segura en hilos.
    """
    db = SessionLocal()
    try:
        print("--- [S3 Cleanup Task Started] ---")

        # 1. Obtener todos los registros de ambas tablas
        all_templates = db.query(models.Template).all()
        all_images = db.query(models.TemplateWithImage).all()
        all_records = all_templates + all_images

        print(f"Found {len(all_records)} total records to check in DB.")

        deleted_count = 0

        for record in all_records:
            if not record.s3_key: # Seguridad por si algún registro tiene clave nula
                continue

            try:
                # 2. Usar 'head_object' es la forma más rápida de verificar si existe
                s3.head_object(Bucket=S3_BUCKET_NAME, Key=record.s3_key)

            except botocore.exceptions.ClientError as e:
                if e.response['Error']['Code'] == '404':
                    # 3. Si S3 da 404 (No Encontrado), el objeto no existe. Borrar de la BD.
                    print(f"Orphaned record found (404): {record.s3_key}. Deleting from DB.")
                    db.delete(record)
                    deleted_count += 1
                else:
                    # Otro error de S3 (ej. 403 Forbidden)
                    print(f"S3 error checking {record.s3_key}: {e}")
            except Exception as e:
                # Otro error inesperado
                print(f"Unexpected error checking {record.s3_key}: {e}")

        # 4. Hacer commit de todas las eliminaciones al final
        if deleted_count > 0:
            db.commit()
            print(f"Committed {deleted_count} deletions from database.")
        else:
            print("No orphaned records found. DB is clean.")

        print("--- [S3 Cleanup Task Finished] ---")

    except Exception as e:
        # Error general en la tarea
        print(f"FATAL ERROR in cleanup task: {e}")
        db.rollback() # Revertir cualquier cambio si la tarea falla a la mitad
    finally:
        db.close() # MUY importante cerrar la sesión de la base de datos
