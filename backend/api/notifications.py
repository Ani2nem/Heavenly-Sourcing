from fastapi import APIRouter, Depends
from sqlmodel import Session, select

from database import get_session
from models import Notification

router = APIRouter(tags=["notifications"])


@router.get("/notifications")
def get_notifications(session: Session = Depends(get_session)):
    """Toast feed: returns unread notifications without mutating state.

    The toast component dedupes by id locally, so we leave ``is_read`` alone
    here — that way the bell's unread count stays accurate until the user
    explicitly clears it via ``mark-all-read``.
    """
    unread = session.exec(
        select(Notification)
        .where(Notification.is_read == False)  # noqa: E712
        .order_by(Notification.created_at.desc())
    ).all()

    return {
        "unread": [
            {
                "id": str(n.id),
                "title": n.title,
                "message": n.message,
                "created_at": n.created_at.isoformat(),
            }
            for n in unread
        ]
    }


@router.get("/notifications/recent")
def get_recent_notifications(limit: int = 20, session: Session = Depends(get_session)):
    """Bell feed: returns recent notifications without mutating read state.

    The sidebar bell uses this so the badge count stays accurate even when the
    transient toasts have already cleared the unread queue. ``unread_count``
    reflects items the user has not explicitly cleared via mark-all-read.
    """
    rows = session.exec(
        select(Notification)
        .order_by(Notification.created_at.desc())
        .limit(limit)
    ).all()
    items = [
        {
            "id": str(n.id),
            "title": n.title,
            "message": n.message,
            "created_at": n.created_at.isoformat(),
            "is_read": bool(n.is_read),
        }
        for n in rows
    ]
    unread_count = sum(1 for n in rows if not n.is_read)
    return {"items": items, "unread_count": unread_count}


@router.post("/notifications/mark-all-read")
def mark_all_read(session: Session = Depends(get_session)):
    rows = session.exec(
        select(Notification).where(Notification.is_read == False)  # noqa: E712
    ).all()
    for n in rows:
        n.is_read = True
        session.add(n)
    session.commit()
    return {"marked_read": len(rows)}
