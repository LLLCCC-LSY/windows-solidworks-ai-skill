"""Keep document ownership attached to native objects, on the COM STA thread."""

import threading
from contextlib import contextmanager

from solidworks_mcp.com import compat
from solidworks_mcp.util import result as R

MUTATIONS = frozenset({
    "create_sketch", "add_sketch_entities", "exit_sketch", "add_sketch_constraint",
    "extrude_feature", "rebuild_document", "edit_sketch", "modify_dimension",
    "save_document", "close_document", "add_sketch_dimension",
    "define_plate_sketch", "link_thickness",
})


class DocumentPolicy:
    def __init__(self, session, identity):
        self.session = session
        self.identity = identity
        self.records = []
        self._local = threading.local()
        self._active_doc = session.active_doc
        self._closed_identity = None
        session.active_doc = self.active_doc
        session.operation_scope = self.scope
        session.on_new_document = lambda doc: self.remember(doc, True)
        # Opening a file never grants write access, even if it was already open.
        session.on_open_document = lambda doc: self.remember(doc, False)

    def active_doc(self):
        pinned = getattr(self._local, "document", None)
        return pinned if pinned is not None else self._active_doc()

    def remember(self, doc, writable):
        identity = self.identity(doc)
        self.records[:] = [(known, allowed) for known, allowed in self.records if known != identity]
        self.records.append((identity, writable))

    def require_active(self, writable=True):
        doc = self.active_doc()
        identity = self.identity(doc)
        if any(known == identity and (allowed or not writable) for known, allowed in self.records):
            return doc
        raise R.ToolError(
            "DOCUMENT_NOT_OWNED", "The active document was not created by this modeling server session.",
            hint="Create a new part with new_document. Existing user documents are query-only.",
        )

    @contextmanager
    def scope(self, operation):
        previous = getattr(self._local, "document", None)
        try:
            if operation in MUTATIONS:
                doc = self.require_active(writable=operation != "close_document")
                if operation == "close_document":
                    self._closed_identity = None
                    closing_identity = self.identity(doc)
                    dirty = compat.pm(doc, "GetSaveFlag", default=None)
                    if dirty is not False and dirty != 0:
                        raise ValueError("Save changes to a new file before closing this document.")
                self._local.document = doc
            yield
            if operation == "close_document":
                self._closed_identity = closing_identity
        finally:
            self._local.document = previous

    def forget_closed(self):
        identity = self._closed_identity
        self.records[:] = [(known, allowed) for known, allowed in self.records if known != identity]
        self._closed_identity = None
