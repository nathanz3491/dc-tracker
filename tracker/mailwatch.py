"""Did this morning's email go out? Asked by the console, which runs all day.

The morning email (`tracker notify send`, scheduled for 08:00 on the production
host) alerts the administrators when something in it failed. Three things it
cannot say, because it is the one that would have to say them:

* **it never ran**: the scheduled job did not start, or the machine was off or
  asleep when it was due;
* **it died partway**: a crash or a kill after it started, before it recorded
  that it finished;
* **its alert could not be sent**: Resend refused the alert along with the email
  it was about.

The console is a different process, started by a different scheduled job, and it
runs all day. After `TRACKER_NOTIFY_DEADLINE` (08:30) it reads the mailer's own
record (`notify_run`, `notify_delivery`) and, when the morning went unreported,
sends the alert in its place, to the same people (`notify.send_alert`). Each kind
of alert goes once a day: one that went is an `alert` row the next look finds.
One that could not be sent is tried again an hour later, so an alert lost to a
Resend outage arrives once Resend is back.

**What it cannot see.** The machine being down takes the console with it; the
tunnel's own health alert covers that (Cloudflare: Notifications, Tunnel Health
Alert, on every plan). And while Resend sends nothing at all, nothing sent
through Resend can say so. Then the missing morning email is the sign, and this
alert follows when Resend is back.
"""

from __future__ import annotations

import datetime as dt
import logging
import threading
from collections.abc import Callable
from contextlib import AbstractContextManager
from typing import Final

from sqlalchemy import select
from sqlalchemy.orm import Session

from tracker import notify
from tracker.models import NotifyDelivery, NotifyRun, utcnow

log = logging.getLogger(__name__)

#: How often the console looks, once the deadline has passed. Cheap: two indexed
#: reads, and nothing at all once the morning is accounted for.
TICK_S: Final[float] = 900.0

#: The wait after an alert that could not be sent, before the next try.
RETRY: Final = dt.timedelta(hours=1)

#: A run that started this long ago and has not finished has died. A normal run
#: takes under a minute.
DIED_AFTER: Final = dt.timedelta(minutes=30)

#: The first look, this long after the console starts, so a restart's own
#: settling is over before it reads anything.
FIRST_LOOK_S: Final[float] = 60.0

_FOOTER: Final = (
    "Anyone who has not had today's email is owed its updates and gets them with the next one."
)

SessionFactory = Callable[[], AbstractContextManager[Session]]


def deadline(text: str) -> dt.time | None:
    """`TRACKER_NOTIFY_DEADLINE` as a time; None when empty, or malformed and logged."""
    text = text.strip()
    if not text:
        return None
    try:
        hour, minute = (int(part) for part in text.split(":"))
        return dt.time(hour, minute)
    except ValueError:
        log.warning(
            "TRACKER_NOTIFY_DEADLINE=%r is not HH:MM, so the morning email is not watched", text
        )
        return None


def _midnight(now: dt.datetime) -> dt.datetime:
    """Today's start in this machine's zone, as the naive UTC the database keeps."""
    start = notify.local_time(now).replace(hour=0, minute=0, second=0, microsecond=0)
    return start.astimezone(dt.UTC).replace(tzinfo=None)


def unreported(
    session: Session, *, now: dt.datetime, due: dt.time
) -> tuple[str, list[notify.Problem]] | None:
    """(subject, problems) for what this morning's run left unsaid, or None.

    None before the deadline, while a run is still going, and on a morning whose run
    finished and either found nothing to report or got its alert out.
    """
    if notify.local_time(now).time() < due:
        return None
    run = session.scalars(
        select(NotifyRun)
        .where(NotifyRun.started_at >= _midnight(now))
        .order_by(NotifyRun.id.desc())
        .limit(1)
    ).first()
    if run is None:
        return "dc-tracker: the morning email did not run today", [
            notify.Problem(
                title="The morning email did not run today",
                why=f"No run had started by {due:%H:%M}: the scheduled job did not start, "
                "or the machine was off or asleep when it was due. Nobody has had "
                "today's email.",
                fix="On the host, `tracker notify send` sends today's now and "
                "`tracker notify status` shows the last run. If it happens again, check "
                "that the scheduled job is still loaded.",
            )
        ]
    started = notify.local_time(run.started_at)
    if run.finished_at is None:
        if now - run.started_at < DIED_AFTER:
            return None
        return "dc-tracker: the morning email stopped partway", [
            notify.Problem(
                title="The morning email stopped partway",
                why=f"The run that started at {started:%H:%M} never finished: it crashed "
                "or was stopped. Anybody it did not reach is owed their updates.",
                fix="On the host, `tracker notify status` shows who was sent what, and "
                "`tracker notify send` sends the rest.",
            )
        ]
    alerts = session.scalars(
        select(NotifyDelivery).where(
            NotifyDelivery.run_id == run.id, NotifyDelivery.kind == "alert"
        )
    ).all()
    if not alerts or any(alert.status == "sent" for alert in alerts):
        return None
    failed = session.scalars(
        select(NotifyDelivery).where(
            NotifyDelivery.run_id == run.id,
            NotifyDelivery.kind != "alert",
            NotifyDelivery.status == "failed",
        )
    ).all()
    problems = [
        notify.Problem(
            title="This morning's alert could not be sent",
            why=f"The {started:%H:%M} run found something to report, and the email "
            "reporting it failed too.",
            fix="`tracker notify status` on the host has the whole record.",
            said=alerts[-1].error,
        )
    ]
    problems += [
        notify.Problem(
            title=f"Could not send to {delivery.email}",
            why=f"Resend would not take it, after {delivery.attempts} attempt(s).",
            said=delivery.error,
        )
        for delivery in failed
    ]
    return "dc-tracker: this morning's alert could not be sent", problems


def already_told(session: Session, subject: str, *, now: dt.datetime) -> bool:
    """Whether an alert with this subject reached anybody today."""
    return (
        session.scalar(
            select(NotifyDelivery.id)
            .where(
                NotifyDelivery.kind == "alert",
                NotifyDelivery.status == "sent",
                NotifyDelivery.subject == subject,
                NotifyDelivery.prepared_at >= _midnight(now),
            )
            .limit(1)
        )
        is not None
    )


class Watchdog(threading.Thread):
    """Looks at the morning email every :data:`TICK_S`, from the console."""

    def __init__(
        self,
        *,
        read: SessionFactory,
        write: SessionFactory,
        due: dt.time,
        transport: Callable[[], notify.Transport] = notify.ResendTransport,
        clock: Callable[[], dt.datetime] = utcnow,
        interval_s: float = TICK_S,
        first_look_s: float = FIRST_LOOK_S,
    ) -> None:
        super().__init__(name="mail-watch", daemon=True)
        self.read = read
        self.write = write
        self.due = due
        self.transport = transport
        self.clock = clock
        self.interval_s = interval_s
        self.first_look_s = first_look_s
        self.halt = threading.Event()
        #: Set when an alert could not be sent: nothing is tried again before it.
        self.retry_at: dt.datetime | None = None

    def look(self) -> list[notify.Outcome]:
        """One look: send the alert when this morning went unreported and untold."""
        now = self.clock()
        if self.retry_at is not None and now < self.retry_at:
            return []
        with self.read() as session:
            found = unreported(session, now=now, due=self.due)
            if found is None or already_told(session, found[0], now=now):
                return []
        subject, problems = found
        with self.write() as session:
            out = notify.send_alert(
                session,
                self.transport(),
                subject=subject,
                intro=f"The console looked at {notify.local_time(now):%H:%M}.",
                found=problems,
                footer=_FOOTER,
            )
        failed = bool(out) and not any(outcome.sent for outcome in out)
        self.retry_at = now + RETRY if failed else None
        return out

    def run(self) -> None:
        if self.halt.wait(self.first_look_s):
            return
        while True:
            try:
                self.look()
            except Exception:  # a look must never take the console down with it
                log.exception("mail watch: a look at the morning email failed")
            if self.halt.wait(self.interval_s):
                return

    def stop(self) -> None:
        self.halt.set()


__all__ = [
    "DIED_AFTER",
    "FIRST_LOOK_S",
    "RETRY",
    "TICK_S",
    "Watchdog",
    "already_told",
    "deadline",
    "unreported",
]
