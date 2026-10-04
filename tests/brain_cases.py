"""Offline check of the second brain: phrasing → command → reply, on a temp DB, no model.

Run: .venv\\Scripts\\python.exe -X utf8 tests\\brain_cases.py
"""

from __future__ import annotations

import sys
import tempfile
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jarvis_bridge import brain as B  # noqa: E402
from jarvis_bridge.bus import EventBus  # noqa: E402

NOW = datetime(2026, 10, 4, 9, 30)  # Sunday

# (text, expected kind/op, optional substring expected in reply)
CASES = [
    # notes
    ("prendi nota che il fornitore nuovo si chiama Rossi", "note/add", "Annotato"),
    ("appunta: idea per il video, parlare dei preset", "note/add", "idea per il video"),
    ("Jarvis, segnati che il wifi dello studio è lento la mattina", "note/add", None),
    ("nota il codice armadietto è 4521", "note/add", None),
    ("cosa ho annotato sul fornitore?", "note/show", "Rossi"),
    ("leggimi le note di oggi", "note/show", "4 note"),
    ("che note ho sul video", "note/show", "preset"),
    # facts
    ("ricordati che Marti è allergica alle noci", "fact/add", "ricorderò"),
    ("tieni a mente che il codice del cancello è 7788", "fact/add", None),
    ("il mio commercialista si chiama Bianchi", "fact/add", None),
    ("ti ricordi a cosa è allergica Marti?", "recall/search", "noci"),
    ("cosa sai del commercialista", "recall/search", "Bianchi"),
    ("come si chiama il mio commercialista?", "none/none", None),  # ordinary chat: memory injected there
    # reminders
    ("ricordami domani alle 9 di chiamare il commercialista", "reminder/add", "domani alle 9:00"),
    ("ricordami di buttare la spazzatura stasera", "reminder/add", "oggi alle 20:00"),
    ("tra 20 minuti ricordami di togliere la pasta", "none/none", None),  # time first: handled by model fallback or rephrase
    ("avvisami tra 20 minuti di togliere la pasta", "reminder/add", "oggi alle 9:50"),
    ("devo pagare la bolletta entro 3 giorni", "reminder/add", "pagare la bolletta"),
    ("segnati che domani ho il dentista alle 16", "reminder/add", "domani alle 16:00"),
    ("promemoria: revisione auto il 12/10 alle 8", "reminder/add", "12 ottobre"),
    ("ricordati di rinnovare il passaporto la settimana prossima", "reminder/add", "rinnovare il passaporto"),
    ("mi devi ricordare di comprare il regalo per Luca", "reminder/add", "Se vuoi un orario"),
    ("cosa devo fare oggi?", "reminder/show", "spazzatura"),
    ("che promemoria ho domani", "reminder/show", "commercialista"),
    ("quali impegni ho questa settimana", "reminder/show", "promemoria"),
    ("cancella il promemoria della spazzatura", "reminder/cancel", "Annullato"),
    ("fatto, ho pagato la bolletta", "reminder/done", "bolletta"),
    # lists
    ("aggiungi il latte alla spesa", "list/add", "latte"),
    ("metti pane, uova e caffè nella lista della spesa", "list/add", "caffè"),
    ("ci servono le batterie", "list/add", "batterie"),
    ("aggiungi trapano alla lista ferramenta", "list/add", "ferramenta"),
    ("cosa c'è nella spesa?", "list/show", "5 elementi"),
    ("leggimi la lista ferramenta", "list/show", "trapano"),
    ("togli le uova dalla spesa", "list/remove", "uova"),
    ("ho comprato il latte dalla spesa", "list/remove", "latte"),
    ("quali liste ho?", "list/names", "spesa"),
    ("svuota la lista spesa", "list/clear", "Dimmi sì o no"),
    ("sì", "list/clear", "svuotata"),
    ("cosa c'è nella spesa", "list/show", "vuota"),
    # forget with confirmation
    ("dimentica che il codice del cancello è 7788", "forget/remove", "Confermi"),
    ("no", "forget/cancelled", "com'è"),
    # recurrence, priority, snooze, evening, first
    ("ricordami ogni lunedì alle 8 di portare fuori il vetro", "reminder/add", "ogni lunedì alle 8:00"),
    ("ogni giorno alle 7 ricordami le vitamine", "none/none", None),  # time-first phrasing: model fallback
    ("ricordami tutti i giorni alle 7 di prendere le vitamine", "reminder/add", "ogni giorno alle 7:00"),
    ("ricordami ogni mese il 27 di pagare l'affitto, è importante", "reminder/add", "ogni mese il 27"),
    ("ricordami urgente di chiamare il medico oggi alle 17", "reminder/add", "urgente"),
    ("da dove partiamo?", "today/first", "Partirei da"),
    ("rimandalo di un'ora", "reminder/snooze", None),
    ("buonanotte", "today/evening", "buonanotte"),
    # summary
    ("buongiorno", "today/summary", "Oggi"),
    ("cosa mi aspetta oggi", "today/summary", None),
    # not for the brain
    ("apri spotify", "none/none", None),
    ("che ore sono?", "none/none", None),
    ("metti un timer di 10 minuti", "none/none", None),
    ("raccontami una barzelletta", "none/none", None),
]


def main() -> int:
    tmp = Path(tempfile.mkdtemp()) / "brain-test.db"
    brain = B.Brain(EventBus(), store=B.Store(tmp), extractor=None)
    fail = 0
    for text, want, needle in CASES:
        out = brain.handle(text, allow_model=False, now=NOW)
        got = f"{out.kind}/{out.op}" if out.handled else "none/none"
        ok = got == want and (needle is None or needle.lower() in out.reply.lower())
        fail += 0 if ok else 1
        print(f"{'PASS' if ok else 'FAIL'} {text!r:62} -> {got:16} {out.reply[:110]}")
    print(f"\n{'ALL PASS' if not fail else f'{fail} FAILED'} ({len(CASES)} cases)")
    return 1 if fail else 0


if __name__ == "__main__":
    raise SystemExit(main())
