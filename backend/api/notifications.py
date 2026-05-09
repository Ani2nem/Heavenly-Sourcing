from fastapi import APIRouter, Depends
from sqlmodel import Session, select

from database import get_session
from models import Notification

router = APIRouter(tags=["notifications"])


@router.get("/notifications")
def get_notifications(session: Session = Depends(get_session)):
    unread = session.exec(
        select(Notification)
        .where(Notification.is_read == False)
        .order_by(Notification.created_at.desc())
    ).all()

    result = [
        {
            "id": str(n.id),
            "title": n.title,
            "message": n.message,
            "created_at": n.created_at.isoformat(),
        }
        for n in unread
    ]

    # Mark as read
    for n in unread:
        n.is_read = True
        session.add(n)
    session.commit()

    return {"unread": result}
