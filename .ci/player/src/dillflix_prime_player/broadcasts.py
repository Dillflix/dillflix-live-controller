"""Read-only, unnormalized live-details evidence for broadcast selection research."""

from datetime import datetime, timezone
import math
import re
import time
import uuid

from .errors import BusyError, EvidenceLostError, StaleResultError, UnavailableError


def assemble(rows, request_id):
    pieces = [r['data'] for r in rows if r.get('kind') == 'broadcasts_piece'
              and r['data'].get('id') == request_id]
    ends = [r['data'] for r in rows if r.get('kind') == 'broadcasts_done'
            and r['data'].get('id') == request_id]
    if not ends:
        return None
    end = ends[-1]
    if end['status'] == 'busy':
        raise BusyError('previous broadcast request is still settling')
    if end['status'] != 'resolved':
        raise UnavailableError('broadcast request failed: ' + end['status'])
    if (len(ends) != 1 or not pieces or len(pieces) > 192
            or len(pieces) != end['pieces']
            or [p['index'] for p in pieces] != list(range(len(pieces)))):
        raise EvidenceLostError('broadcast response pieces were lost or duplicated')
    branches, seen, resource = {}, set(), None
    for p in pieces:
        path, branch = p.get('path'), p.get('branch')
        if (not isinstance(path, list) or len(path) > 32
                or any(type(k) not in (str, int) for k in path)
                or branch not in (None, 'array', 'object')):
            raise EvidenceLostError('invalid broadcast response path')
        key = tuple(path)
        if key in seen:
            raise EvidenceLostError('duplicate broadcast response path')
        seen.add(key)
        value = [] if branch == 'array' else {} if branch == 'object' else p['body']
        if not path:
            resource = value
        else:
            parent = branches.get(tuple(path[:-1]))
            leaf = path[-1]
            if isinstance(parent, list) and type(leaf) is int and leaf == len(parent):
                parent.append(value)
            elif isinstance(parent, dict) and type(leaf) is str and leaf not in parent:
                parent[leaf] = value
            else:
                raise EvidenceLostError('invalid broadcast response ordering')
        if branch:
            branches[key] = value
    if not isinstance(resource, dict) or 'containerList' not in resource:
        raise EvidenceLostError('missing live-details containerList')
    return resource, end.get('warnings', ())


def fetch(controller, content_id, timeout, ownership):
    if not isinstance(content_id, str) or not re.fullmatch(r'amzn1\.dv\.gti\.[A-Za-z0-9-]+', content_id):
        raise ValueError('a GTI content_id is required')
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 5 <= timeout <= 120:
        raise ValueError('broadcast timeout must be 5..120 seconds')
    with controller.operation(ownership, readonly=True):
        if controller._active_attempts():
            raise BusyError('finish playback verification before requesting broadcasts')
        controller._require('broadcasts')
        deadline = time.monotonic() + timeout
        b = controller.backend
        # Read-only inspection is also available under acknowledged manual ownership.
        b.wait(lambda: b.health()['state'].get('ready'), min(20, timeout))
        generation = controller._generation()
        cursor, request_id = b.cursor, uuid.uuid4().hex
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise UnavailableError('broadcast readiness deadline exceeded')
        b.command('broadcasts-data', command_id=request_id, contentId=content_id,
                  requestTimeoutMs=max(1, int(remaining * 1000)), timeout=min(30, remaining))
        try:
            resource, warnings = b.wait(lambda: assemble(b.records(cursor), request_id),
                                        max(0, deadline - time.monotonic()))
        except TimeoutError as exc:
            raise UnavailableError('broadcast request timed out; request may still be settling') from exc
        if controller._generation() != generation:
            raise StaleResultError('runtime changed during broadcast request')
        return dict(content_id=content_id, request_id=request_id, session_id=b.session_id,
                    generation=generation, observed_at=datetime.now(timezone.utc).isoformat(),
                    resource=resource, warnings=list(warnings), complete=True,
                    coverage='live_details_btf_response')
