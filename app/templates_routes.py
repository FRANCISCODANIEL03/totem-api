import uuid
from io import BytesIO
from PIL import Image, ImageOps
import boto3
import botocore
from botocore.config import Config as BotocoreConfig
from fastapi import APIRouter, UploadFile, File, Depends, HTTPException, BackgroundTasks
from fastapi.responses import StreamingResponse
from sqlalchemy import or_
from sqlalchemy.orm import Session
from pydantic import BaseModel
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

class PromptRequest(BaseModel):
    prompt: str

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

@router.get("/public")
def list_public_templates(db: Session = Depends(get_db)):
    """Devuelve todas las plantillas marcadas como públicas (Globales)"""
    templates = db.query(models.Template).filter(models.Template.is_public == True).all()
    
    result = []
    # Usamos un ID genérico o 'system' en la URL si el user_id es nulo
    for t in templates:
        # Si el template no tiene usuario (es del sistema), ajustamos la ruta
        folder = t.user_id if t.user_id else "system"
        
        proxy_url = f"{URL_PRODUCTION}/templates/image/{folder}/{t.id}.png"
        
        result.append({
            "uuid": t.id,
            "s3_key": t.s3_key,
            "url": proxy_url,
            "is_public": True
        })
    return result


# ==============================
# 3️⃣ INTEGRAR PERSONA EN TEMPLATE
# ==============================
# @router.post("/integrate/{template_id}")
# async def integrate_person(
#     template_id: str,
#     background_tasks: BackgroundTasks,
#     file: UploadFile = File(...),
#     db: Session = Depends(get_db),
#     current_user=Depends(get_current_user)
# ):
#     # Verificar que la plantilla exista
#     # template = db.query(models.Template).filter_by(id=template_id, user_id=current_user.id).first()
#     # if not template:
#     #     raise HTTPException(status_code=404, detail="Template not found")

#     # Buscar el template si pertenece al usuario O si es público
#     template = db.query(models.Template).filter(
#         models.Template.id == template_id,
#         or_(
#             models.Template.user_id == current_user.id,
#             models.Template.is_public == True
#         )
#     ).first()

#     if not template:
#         raise HTTPException(status_code=404, detail="Template not found or access denied")
    
#     # Generar nuevo UUID para la imagen resultante
#     new_uid = str(uuid.uuid4())
#     s3_key = f"{current_user.id}/{new_uid}.png"

#     # Guardar el registro de TemplateWithImage
#     template_with_image = models.TemplateWithImage(
#         id=new_uid,
#         user_id=current_user.id,
#         s3_key=s3_key,
#         template_id=template.id
#     )
#     db.add(template_with_image)
#     db.commit()
#     db.refresh(template_with_image)

#     # Leer el archivo antes de liberar la conexión
#     contents = await file.read()

#     # Lanzar tarea en background
#     background_tasks.add_task(process_and_integrate_person, template.s3_key, contents, s3_key)

#     # Responder rápido
#     return {"uuid": new_uid, "status": "processing", "user_id": current_user.id}

@router.post("/admin/generate-public-template")
async def generate_public_template(
    request: PromptRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user) 
    # Idealmente, aquí verificarías si current_user es admin
):
    uid = str(uuid.uuid4())
    # Guardamos en una carpeta "system" o en la del admin, pero marcamos como público
    s3_key = f"system/{uid}.png" 

    # Crear registro en BD como PÚBLICO
    template = models.Template(
        id=uid, 
        user_id=None, # O usa current_user.id si prefieres que tenga dueño
        s3_key=s3_key, 
        is_public=True 
    )
    db.add(template)
    db.commit()

    # Llamamos a una tarea para generar la imagen con Gemini
    background_tasks.add_task(generate_and_upload_public_template, request.prompt, s3_key)

    return {"uuid": uid, "status": "generating_public_template"}

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

@router.get("/image/{folder}/{filename}")
def get_template_image(folder: str, filename: str):
    """Devuelve la imagen desde S3 a través del backend con cabeceras CORS."""
    s3_key = f"{folder}/{filename}"

    buffer = BytesIO()
    try:
        s3.download_fileobj(S3_BUCKET_NAME, s3_key, buffer)
    except Exception as e:
        # Asegúrate de que las respuestas de error también tienen CORS si devuelven JSON
        raise HTTPException(
            status_code=404,
            detail=f"Image not found: {str(e)}",
            headers={"Access-Control-Allow-Origin": "*"}
        )

    buffer.seek(0)

    # --- AÑADIR CABECERAS CORS A LA RESPUESTA DE STREAMING ---
    headers = {
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "GET",
        "Access-Control-Allow-Headers": "*"
    }

    return StreamingResponse(
        buffer,
        media_type="image/png",
        headers=headers # Incluye las cabeceras en la respuesta
    )

def process_and_upload_template(contents: bytes, s3_key: str, user_id: str):
    try:
        img = load_image_corrected(contents)

        CANVAS_WIDTH = 1080
        CANVAS_HEIGHT = 1350

        prompt = f"""
        You are designing a SOLID PHOTO FRAME TEMPLATE.

        MANDATORY RULES:
        1. Orientation: PORTRAIT (vertical).
        2. Canvas size: 1080x1350 pixels.
        3. The frame must be SOLID and CONTINUOUS.
        4. The frame must touch all four edges of the canvas.
        5. No white margins. No padding.

        GEOMETRY RULES (VERY IMPORTANT):
        - The decorative frame must be drawn at FULL SCALE.
        - No small or centered frames.
        - The frame must extend edge-to-edge.

        COLOR RULES:
        - Do NOT use plain white as the main frame color.
        - Ignore white backgrounds in the reference image.
        - Use saturated colors from the reference image.

        STYLE:
        - Inspired by the reference image.
        - Professional, realistic, printed photo frame.
        """

        #  Generar marco sólido
        result_img = process_with_gemini(
            prompt,
            img
        )

        # 2. Forzar que ocupe todo el canvas
        result_img = ensure_frame_fills_canvas(result_img)

        #  Normalizar tamaño PRIMERO
        result_img = result_img.resize(
            (CANVAS_WIDTH, CANVAS_HEIGHT),
            Image.Resampling.LANCZOS
        )

        # 3️⃣ APLICAR ventana transparente (AQUÍ Y SOLO AQUÍ)
        result_img = apply_fixed_transparent_window(result_img)

        # 4️⃣ Guardar
        buffer = BytesIO()
        result_img.save(buffer, format="PNG")
        buffer.seek(0)

        s3.put_object(
            Bucket=S3_BUCKET_NAME,
            Key=s3_key,
            Body=buffer,
            ContentType="image/png",
            ACL="public-read"
        )

        print(f"✅ Template generated and uploaded: {s3_key}")

    except Exception as e:
        print(f"❌ Error generating template: {str(e)}")


def ensure_frame_fills_canvas(img: Image.Image, min_coverage=0.9) -> Image.Image:
    img = img.convert("RGBA")
    w, h = img.size
    pixels = img.getdata()

    non_white = sum(
        1 for r, g, b, a in pixels
        if a > 10 and not (r > 240 and g > 240 and b > 240)
    )

    coverage = non_white / (w * h)

    # Si el marco ocupa muy poco → escalarlo
    if coverage < min_coverage:
        # Crop al área no blanca
        bbox = img.getbbox()
        if bbox:
            cropped = img.crop(bbox)
            return cropped.resize((w, h), Image.Resampling.LANCZOS)

    return img


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

def generate_and_upload_public_template(prompt_theme: str, s3_key: str):
    try:
        CANVAS_WIDTH = 1080
        CANVAS_HEIGHT = 1350

        full_prompt = f"""
        You are designing a SOLID PHOTO FRAME TEMPLATE.

        THEME:
        {prompt_theme}

        MANDATORY RULES:
        1. Orientation: PORTRAIT (vertical).
        2. Canvas size: 1080x1350 pixels.
        3. The frame must be SOLID and CONTINUOUS.
        4. The frame must touch all four edges of the canvas.
        5. No white margins. No padding.
        6. No holes, no gaps, no floating elements.
        7. The center must be clean (no people, no text).

        STYLE RULES:
        - Use strong, saturated colors.
        - Avoid plain white or empty backgrounds.
        - The frame should clearly express the theme.
        - Professional, realistic, printed photo frame.

        GEOMETRY RULES (VERY IMPORTANT):
        - Draw the frame at FULL SCALE.
        - No small or centered frames.
        - Edge-to-edge design.
        """

        # 1️⃣ Generar marco sólido (solo con prompt)
        result_img = process_with_gemini(full_prompt)

        # 2️⃣ Forzar que ocupe todo el canvas
        result_img = ensure_frame_fills_canvas(result_img)

        # 3️⃣ Normalizar tamaño
        result_img = result_img.resize(
            (CANVAS_WIDTH, CANVAS_HEIGHT),
            Image.Resampling.LANCZOS
        )

        # 4️⃣ Aplicar ventana transparente fija (MISMA que privadas)
        result_img = apply_fixed_transparent_window(result_img)

        # 5️⃣ Guardar
        buffer = BytesIO()
        result_img.save(buffer, format="PNG")
        buffer.seek(0)

        s3.put_object(
            Bucket=S3_BUCKET_NAME,
            Key=s3_key,
            Body=buffer,
            ContentType="image/png",
            ACL="public-read"
        )

        print(f"✅ Public template created: {s3_key}")

    except Exception as e:
        print(f"❌ Error generating public template: {str(e)}")


def load_image_corrected(bytes_data: bytes) -> Image.Image:
    img = Image.open(BytesIO(bytes_data))
    img = ImageOps.exif_transpose(img)
    return img.convert("RGB")

def integrate_photo_with_frame(frame_img: Image.Image, person_img: Image.Image) -> Image.Image:
    frame = frame_img.convert("RGBA")
    person = person_img.convert("RGBA")

    w, h = frame.size

    x0 = FRAME_THICKNESS_X
    y0 = FRAME_THICKNESS_TOP
    x1 = w - FRAME_THICKNESS_X
    y1 = h - FRAME_THICKNESS_BOTTOM

    hole_w = x1 - x0
    hole_h = y1 - y0

    # scale = min(hole_w / person.width, hole_h / person.height)
    scale = max(hole_w / person.width, hole_h / person.height)
    new_w = int(person.width * scale)
    new_h = int(person.height * scale)

    person_resized = person.resize((new_w, new_h), Image.Resampling.LANCZOS)

    canvas = Image.new("RGBA", (w, h), (0, 0, 0, 0))

    px = x0 + (hole_w - new_w) // 2
    py = y0 + (hole_h - new_h) // 2

    canvas.paste(person_resized, (px, py), person_resized)
    canvas.paste(frame, (0, 0), frame)

    return canvas


# 🔧 CONFIGURACIÓN GLOBAL (UNA SOLA VEZ)
CANVAS_WIDTH = 1080
CANVAS_HEIGHT = 1350

FRAME_THICKNESS_X = 120
FRAME_THICKNESS_TOP = 160
FRAME_THICKNESS_BOTTOM = 200


def apply_fixed_transparent_window(frame_img: Image.Image) -> Image.Image:
    frame = frame_img.convert("RGBA")
    w, h = frame.size

    x0 = FRAME_THICKNESS_X
    y0 = FRAME_THICKNESS_TOP
    x1 = w - FRAME_THICKNESS_X
    y1 = h - FRAME_THICKNESS_BOTTOM

    alpha = Image.new("L", (w, h), 255)

    transparent_area = Image.new(
        "L",
        (x1 - x0, y1 - y0),
        0
    )

    alpha.paste(transparent_area, (x0, y0))
    frame.putalpha(alpha)

    return frame
