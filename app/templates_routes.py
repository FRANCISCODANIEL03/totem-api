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
@router.post("/integrate/{template_id}")
async def integrate_person(
    template_id: str,
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user)
):
    # Verificar que la plantilla exista
    # template = db.query(models.Template).filter_by(id=template_id, user_id=current_user.id).first()
    # if not template:
    #     raise HTTPException(status_code=404, detail="Template not found")

    # Buscar el template si pertenece al usuario O si es público
    template = db.query(models.Template).filter(
        models.Template.id == template_id,
        or_(
            models.Template.user_id == current_user.id,
            models.Template.is_public == True
        )
    ).first()

    if not template:
        raise HTTPException(status_code=404, detail="Template not found or access denied")
    
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
    return {"uuid": new_uid, "status": "processing", "user_id": current_user.id}

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
    background_tasks.add_task(generate_and_upload_base_frame, request.prompt, s3_key)

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
    """
    Toma una imagen de referencia (tema) y genera un MARCO/PLANTILLA basado en ella.
    Subir el resultado a S3.
    """
    try:
        img = Image.open(BytesIO(contents))

        # 2. Corregir orientación

        # 🔄 NUEVO PROMPT: De "Tema" a "Plantilla/Marco"
        prompt = """
        Analyze the provided image to understand its theme, style, color palette, and visual elements.
        Based on this analysis, generate a photo frame or border template.

        Strict Requirements:
        1. **Layout**: Create a decorative frame that occupies the outer edges of the image.
        2. **Center**: The center area must be a large, clean, empty WHITE space (rectangular or square) intended for a user to insert their own photo later.
        3. **Style Integration**: Use the motifs, textures, and objects from the input image to design the frame (e.g., if the input is floral, make a floral border; if it's neon, make a neon border).
        4. **No Obstructions**: Do NOT generate any people, faces, or text inside the central empty space.
        5. **Full Bleed**: The frame should extend to the very edges of the canvas without external padding.
        
        Output ONLY the frame with the empty center.
        """

        # Procesar con Gemini (Imagen + Prompt)
        result_img = process_with_gemini(prompt, img)

        # Subir a S3
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
        print(f"✅ Template generated from theme and uploaded to: {s3_key}")

    except Exception as e:
        print(f"❌ Error generating template in background task: {str(e)}")
        
def process_and_integrate_person(template_s3_key: str, person_bytes: bytes, output_s3_key: str):
    """
    Se ejecuta en segundo plano. 
    Toma una 'Plantilla/Marco' y una 'Foto de Persona', e inserta la persona dentro del marco.
    """
    try:
        # 1. Descargar el MARCO (Template) desde S3
        base_buffer = BytesIO()
        s3.download_fileobj(S3_BUCKET_NAME, template_s3_key, base_buffer)
        base_buffer.seek(0)
        base_img = Image.open(base_buffer).convert("RGB") # Asegurar formato consistente

        # 2. Cargar la FOTO DEL USUARIO
        person_img = Image.open(BytesIO(person_bytes)).convert("RGB")

        person_img = crop_to_4_5_portrait(person_img)


        # 3. Prompt de Composición (Frame + Photo)
        prompt = """
        You are an expert photo compositor. Your task is to insert the provided user photo into the decorative frame.

        Inputs:
        - Image 1: A decorative frame/border with a large empty or white central area.
        - Image 2: A photo of a person or people.

        Instructions:
        1. **Identify the Void**: Locate the central empty/white space in the decorative frame.
        2. **Insert & Scale**: Place the person/subjects from Image 2 into that central space. Resize the person so they fill the frame's opening naturally, ensuring their faces are clearly visible and centered.
        3. **Preserve the Frame**: Do NOT modify, distort, or obscure the decorative border elements. The frame must remain exactly as it is in the original image.
        4. **Harmonize**: Adjust the lighting, color temperature, and contrast of the inserted person to match the style and lighting of the frame (e.g., if the frame is soft/pastel, soften the photo slightly; if the frame is vibrant, keep the photo vibrant).
        5. **Blending**: Ensure the edges where the photo meets the frame are clean. There should be no white gaps or awkward overlaps.
        
        Output:
        A single final image containing the original decorative frame with the user's photo perfectly composited inside the center.
        """

        # 4. Procesar con Gemini (Enviamos ambas imágenes)
        # Enviamos: [Prompt, Marco, Foto_Persona]
        result_img = process_with_gemini(prompt, base_img, other_image=person_img)

        # 5. Guardar el resultado en memoria
        buffer = BytesIO()
        result_img.save(buffer, format="PNG")
        buffer.seek(0)

        # 6. Subir la imagen final a S3
        s3.put_object(
            Bucket=S3_BUCKET_NAME,
            Key=output_s3_key,
            Body=buffer,
            ContentType="image/png",
            ACL="public-read"
        )

        print(f"✅ Uploaded integrated frame to S3: {output_s3_key}")

    except Exception as e:
        print(f"❌ Error integrating frame for key {output_s3_key}: {str(e)}")

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

def generate_and_upload_base_frame(prompt_text: str, s3_key: str):
    """Genera una plantilla en formato RETRATO 4:5."""
    try:
        # 1. CAMBIO CLAVE: Lienzo 4:5 (1080x1350)
        base_canvas = Image.new('RGB', (1080, 1350), color='white')
        
        # 2. Prompt ajustado al nuevo ratio
        full_prompt = f"""
        {prompt_text}
        
        CRITICAL FORMAT INSTRUCTIONS:
        1. OUTPUT FORMAT: Standard Portrait aspect ratio 4:5.
        2. LAYOUT: The decorative frame must extend to the EXTREME EDGES of the image.
        3. NO MARGINS: Do NOT generate any white padding or borders outside the frame.
        4. CENTER: The central area must be a large, empty SOLID WHITE rectangle meant for a photo insertion.
        """
        
        # 3. Generar
        result_img = process_with_gemini(full_prompt, base_canvas)

        # 4. Lógica de Transparencia 
        result_img = result_img.convert("RGBA")
        datas = result_img.getdata()
        new_data = []
        threshold = 230 
        for item in datas:
            if item[0] > threshold and item[1] > threshold and item[2] > threshold:
                new_data.append((255, 255, 255, 0)) 
            else:
                new_data.append(item)
        result_img.putdata(new_data)

        # 5. Guardar
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
        print(f"✅ Public transparent template created: {s3_key}")

    except Exception as e:
        print(f"❌ Error generating public template: {str(e)}")


def crop_to_4_5_portrait(img: Image.Image) -> Image.Image:
    """
    Recorta la imagen al formato estándar de retrato 4:5 (1080x1350).
    """
    
    # NUEVAS DIMENSIONES OBJETIVO (Ratio 4:5)
    target_width = 1080
    target_height = 1350

    # ImageOps.fit redimensiona y recorta al centro para llenar estas dimensiones
    img_cropped = ImageOps.fit(
        img, 
        (target_width, target_height), 
        method=Image.Resampling.LANCZOS, 
        centering=(0.5, 0.5)
    )
    
    return img_cropped