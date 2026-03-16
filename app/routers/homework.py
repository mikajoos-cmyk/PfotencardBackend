from fastapi import APIRouter, Depends, HTTPException, status, UploadFile, File
from sqlalchemy.orm import Session
from typing import List, Optional
from .. import crud, models, schemas, auth, storage_service
from ..database import get_db

router = APIRouter(prefix="/api/homework", tags=["homework"])

# --- EXERCISE TEMPLATES (Trainingskatalog) ---

@router.post("/templates", response_model=schemas.ExerciseTemplate)
def create_template(
    template_in: schemas.ExerciseTemplateCreate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_active_user)
):
    if current_user.role not in ['admin', 'mitarbeiter']:
        raise HTTPException(status_code=403, detail="Nicht berechtigt")
    return crud.create_exercise_template(db, current_user.tenant_id, template_in)

@router.get("/templates", response_model=List[schemas.ExerciseTemplate])
def get_templates(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_active_user)
):
    return crud.get_exercise_templates(db, current_user.tenant_id)

@router.put("/templates/{template_id}", response_model=schemas.ExerciseTemplate)
def update_template(
    template_id: int,
    template_in: schemas.ExerciseTemplateUpdate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_active_user)
):
    if current_user.role not in ['admin', 'mitarbeiter']:
        raise HTTPException(status_code=403, detail="Nicht berechtigt")
    
    template = crud.get_exercise_template(db, template_id)
    if not template or template.tenant_id != current_user.tenant_id:
        raise HTTPException(status_code=404, detail="Template nicht gefunden")
        
    return crud.update_exercise_template(db, template_id, template_in)

@router.delete("/templates/{template_id}")
def delete_template(
    template_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_active_user)
):
    if current_user.role not in ['admin', 'mitarbeiter']:
        raise HTTPException(status_code=403, detail="Nicht berechtigt")
    
    template = crud.get_exercise_template(db, template_id)
    if not template or template.tenant_id != current_user.tenant_id:
        raise HTTPException(status_code=404, detail="Template nicht gefunden")
        
    crud.delete_exercise_template(db, template_id)
    return {"ok": True}

# --- HOMEWORK ASSIGNMENTS (Zuweisungen) ---

@router.post("/assign", response_model=schemas.HomeworkAssignment)
def assign_homework(
    assignment_in: schemas.HomeworkAssignmentCreate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_active_user)
):
    if current_user.role not in ['admin', 'mitarbeiter']:
        raise HTTPException(status_code=403, detail="Nicht berechtigt")
    
    # Sicherstellen, dass der Ziel-User zum gleichen Tenant gehört
    target_user = db.query(models.User).filter(models.User.id == assignment_in.user_id).first()
    if not target_user or target_user.tenant_id != current_user.tenant_id:
        raise HTTPException(status_code=400, detail="Ungültiger Ziel-User")
        
    return crud.create_homework_assignment(db, current_user.tenant_id, current_user.id, assignment_in)

@router.get("/user/{user_id}", response_model=List[schemas.HomeworkAssignment])
def get_user_homework(
    user_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_active_user)
):
    # Zugriffserlaubnis prüfen: Admin/Mitarbeiter des Tenants oder der User selbst
    target_user = db.query(models.User).filter(models.User.id == user_id).first()
    if not target_user:
        raise HTTPException(status_code=404, detail="User nicht gefunden")
        
    if current_user.role in ['admin', 'mitarbeiter']:
        if current_user.tenant_id != target_user.tenant_id:
            raise HTTPException(status_code=403, detail="Nicht berechtigt")
    elif current_user.id != user_id:
        raise HTTPException(status_code=403, detail="Nicht berechtigt")
        
    return crud.get_user_homework(db, user_id)

@router.patch("/{assignment_id}/complete", response_model=schemas.HomeworkAssignment)
def complete_homework(
    assignment_id: int,
    completion_in: schemas.HomeworkCompletionRequest,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_active_user)
):
    assignment = crud.get_homework_assignment(db, assignment_id)
    if not assignment:
        raise HTTPException(status_code=404, detail="Hausaufgabe nicht gefunden")
    
    # Nur der zugewiesene User darf abschließen
    if assignment.user_id != current_user.id:
         raise HTTPException(status_code=403, detail="Nicht berechtigt")
         
    return crud.complete_homework_assignment(db, assignment_id, completion_in)

# --- FILE UPLOAD ---

@router.post("/upload")
async def upload_homework_files(
    files: List[UploadFile] = File(...),
    current_user: models.User = Depends(auth.get_current_active_user),
    db: Session = Depends(get_db)
):
    if current_user.role not in ['admin', 'mitarbeiter']:
        raise HTTPException(status_code=403, detail="Nicht berechtigt")
        
    results = []
    for file in files:
        # Speicherpfad: homework/{current_user.tenant_id}/{file.filename}
        file_path = f"homework/{current_user.tenant_id}/{file.filename}"
        file_url = await storage_service.upload_file_to_storage(file, file_path)
        
        file_type = "file"
        if file.content_type:
            if file.content_type.startswith("video/"):
                file_type = "video"
            elif file.content_type.startswith("image/"):
                file_type = "image"
            elif "pdf" in file.content_type:
                file_type = "pdf"
            elif "presentation" in file.content_type or "powerpoint" in file.content_type:
                file_type = "pptx"

        results.append({
            "file_url": file_url, 
            "file_name": file.filename,
            "type": file_type
        })
    
    # Rückwärtskompatibilität für das Frontend (gibt das erste Element auch einzeln zurück, falls nur eins da ist)
    # Aber eigentlich besser, wenn das Frontend die Liste verarbeitet.
    if len(results) == 1:
        return {**results[0], "all_files": results}
    
    return {"all_files": results, "file_url": results[0]["file_url"] if results else None}
