"""An in-memory smo-teiv for TE&IV's tests (plan tasks S3.19 to S3.21): the part of smo-teiv 0.3.0's exposure API TE&IV calls, as an
`httpx.MockTransport` handler, holding what TE&IV's own encoder writes (`app.ingest.encode`: the ingestion events and the updateDecorator calls), so a
test reads back through the facade exactly what TE&IV would have written into the real one.

What it answers, in smo-teiv's shapes (taken from 0.3.0's answers):

  GET  /domains                                                       the domain list
  GET  /domains/{d}/entity-types/{T}/entities/{id}                    `{"module:T": [entity]}`, 404 NOT_FOUND
  GET  /domains/{d}/entity-types/{T}/entities                         `{"items": [{"module:T": [entity]}, ...], "totalCount": n}`, paged
  GET  /domains/{d}/relationship-types/{R}/relationships              the same for relationships
  GET  /user-defined-schemas/{name}/content, POST /user-defined-schemas  (the upload must be application/yang, as smo-teiv requires)
  POST /manage-decorators                                             merge / delete, at most 100 entities (smo-teiv's batch limit)
  anything else                                                       404, or the answer a test put in `answers`

The scopeFilter it understands is the one TE&IV sends: `;`-separated terms, all of which must hold (smo-teiv's AND), each one of
`/sourceIds[contains(@item,'x')]` (x in the JSON text of the sourceIds, as smo-teiv's `"CD_sourceIds"::text LIKE '%x%'`), `/attributes[@a=v]`,
`/attributes[@a='v']` and `/decorators[@m:k='v']`; a relationship always shows its id and sides, as smo-teiv's do. A type in the wrong domain is 400, as smo-teiv answers it. `requests` records what was asked;
`fail_with` makes every call answer that status (a 5xx: smo-teiv down), and `delay` makes every call raise a timeout.
"""

import json
import re
from collections import defaultdict
from urllib.parse import unquote

import httpx

API = "/topology-inventory/v1"
DOMAIN_OF = {"o-ran-smo-teiv-ran": "RAN", "o-ran-smo-teiv-oam": "OAM", "smo-teiv-ext-ran": "EXT_RAN", "smo-teiv-ext-oam": "EXT_OAM"}
TERM = re.compile(r"^/(sourceIds|attributes|decorators)\[(.*)\]$")


def _problem(status: int, title: str, detail: str) -> httpx.Response:
    return httpx.Response(status, json={"type": "about:blank", "title": title, "status": status, "detail": detail, "instance": ""})


class FakeTeiv:
    """The fake (module description); `load` takes what `app.ingest.encode` returns."""

    def __init__(self):
        self.entities: dict[str, dict[str, dict]] = defaultdict(dict)          # module:Type -> id -> entity
        self.relationships: dict[str, dict[str, dict]] = defaultdict(dict)     # module:TYPE -> id -> relationship
        self.schemas: dict[str, bytes] = {}
        self.requests: list[httpx.Request] = []
        self.answers: dict[tuple[str, str], httpx.Response] = {}
        self.fail_with: int | None = None
        self.timeout = False

    def load(self, events: list[dict], decorator_calls: list[dict] = ()) -> "FakeTeiv":
        """Applies ingestion events (entities and relationships, merged by id) and updateDecorator calls."""
        for event in events:
            for block in event.get("entities", []):
                for kind, items in block.items():
                    for item in items:
                        self.entities[kind][item["id"]] = {**self.entities[kind].get(item["id"], {}), **item}
            for block in event.get("relationships", []):
                for kind, items in block.items():
                    for item in items:
                        self.relationships[kind][item["id"]] = dict(item)
        for call in decorator_calls:
            self._decorate(call)
        return self

    def transport(self) -> httpx.MockTransport:
        """The transport to give SmoTeivClient."""
        return httpx.MockTransport(self.handle)

    # ---- the API

    def handle(self, request: httpx.Request) -> httpx.Response:
        """One request (module description)."""
        self.requests.append(request)
        if self.timeout:
            raise httpx.ReadTimeout("fake smo-teiv timed out", request=request)
        if self.fail_with is not None:
            return _problem(self.fail_with, "INTERNAL_SERVER_ERROR", "fake smo-teiv failing")
        path = unquote(request.url.path)
        if not path.startswith(API):
            return _problem(404, "NOT_FOUND", path)
        path = path[len(API):]
        if (request.method, path) in self.answers:
            return self.answers[(request.method, path)]
        params = dict(request.url.params)
        parts = path.strip("/").split("/")
        if request.method == "GET" and parts == ["domains"]:
            return httpx.Response(200, json={"items": [{"domainName": d} for d in sorted(set(DOMAIN_OF.values()))]})
        if request.method == "GET" and len(parts) == 5 and parts[0] == "domains" and parts[2] == "entity-types" and parts[4] == "entities":
            return self._list(parts[1], parts[3], self.entities, params)
        if request.method == "GET" and len(parts) == 6 and parts[0] == "domains" and parts[2] == "entity-types" and parts[4] == "entities":
            return self._one(parts[1], parts[3], parts[5])
        if request.method == "GET" and len(parts) == 5 and parts[0] == "domains" and parts[2] == "relationship-types":
            return self._list(parts[1], parts[3], self.relationships, params, always=("aSide", "bSide"))
        if parts[0] == "user-defined-schemas":
            return self._schemas(request, parts)
        if request.method == "POST" and parts == ["manage-decorators"]:
            body = json.loads(request.content)
            if len(body.get("entityIds", [])) > 100:
                return _problem(400, "BAD_REQUEST", "Number of entity IDs exceeded the limit of 100")
            self._decorate(body)
            return httpx.Response(204)
        return _problem(404, "NOT_FOUND", path)

    def _kind(self, domain: str, name: str, store: dict) -> str | None:
        for kind in store:
            module, short = kind.split(":", 1)
            if short == name and DOMAIN_OF.get(module) == domain:
                return kind
        return None

    def _one(self, domain: str, entity_type: str, identifier: str) -> httpx.Response:
        kind = self._kind(domain, entity_type, self.entities)
        entity = self.entities[kind].get(identifier) if kind else None
        if entity is None:
            return _problem(404, "NOT_FOUND", f"The requested resource is not found. ID: {identifier}")
        return httpx.Response(200, json={kind: [self._shown(entity, None)]})

    def _list(self, domain: str, name: str, store: dict, params: dict, always: tuple[str, ...] = ()) -> httpx.Response:
        kind = self._kind(domain, name, store)
        if kind is None:
            return _problem(400, "BAD_REQUEST", f"{name} is not part of the domain {domain}")
        try:
            terms = [t for t in (params.get("scopeFilter") or "").split(";") if t]
            chosen = [item for _, item in sorted(store[kind].items()) if all(self._holds(term, item) for term in terms)]
        except ValueError as exc:
            return _problem(400, "BAD_REQUEST", str(exc))
        offset, limit = int(params.get("offset", 0)), int(params.get("limit", 500))
        fields = params.get("targetFilter")
        page = chosen[offset:offset + limit]
        return httpx.Response(200, json={"items": [{kind: [self._shown(item, fields, always)]} for item in page], "totalCount": len(chosen)})

    @staticmethod
    def _shown(item: dict, fields: str | None, always: tuple[str, ...] = ()) -> dict:
        """What smo-teiv shows of an item for a targetFilter: the id (and a relationship's sides) always, then the fields named."""
        if not fields:
            return {k: v for k, v in item.items()}
        keep = {"id", *always} | {f.strip("/") for f in fields.split(";")}
        out = {k: v for k, v in item.items() if k in keep}
        if "decorators" in keep:
            out.setdefault("decorators", {})
        return out

    @staticmethod
    def _holds(term: str, item: dict) -> bool:
        m = TERM.match(term)
        if not m:
            raise ValueError(f"Invalid scopeFilter term {term}")
        where, cond = m.groups()
        if where == "sourceIds":
            needle = re.fullmatch(r"contains\(@item,'(.*)'\)", cond)
            if not needle:
                raise ValueError(f"Invalid sourceIds condition {cond}")
            return needle.group(1) in json.dumps(item.get("sourceIds", []))
        eq = re.fullmatch(r"@([\w:.-]+)=(?:'(.*)'|(-?\d+))", cond)
        if not eq:
            raise ValueError(f"Invalid condition {cond}")
        name, text, number = eq.groups()
        held = (item.get(where) or {}).get(name)
        return held == (text if text is not None else int(number))

    def _schemas(self, request: httpx.Request, parts: list[str]) -> httpx.Response:
        if request.method == "GET" and len(parts) == 3 and parts[2] == "content":
            if parts[1] in self.schemas:
                return httpx.Response(200, content=self.schemas[parts[1]], headers={"content-type": "text/plain"})
            return _problem(404, "NOT_FOUND", parts[1])
        if request.method == "POST" and len(parts) == 1:
            body = request.content
            if b"Content-Type: application/yang" not in body:
                return _problem(400, "BAD_REQUEST", "Invalid file")
            name = re.search(rb"^module ([\w-]+)", body.split(b"\r\n\r\n", 1)[1], re.M)
            self.schemas[name.group(1).decode()] = body
            return httpx.Response(201)
        return _problem(404, "NOT_FOUND", "/".join(parts))

    def _decorate(self, call: dict) -> None:
        for kind in self.entities:
            for identifier in call["entityIds"]:
                entity = self.entities[kind].get(identifier)
                if entity is None:
                    continue
                decorators = entity.setdefault("decorators", {})
                if call.get("operation") == "delete":
                    for k in call["decorators"]:
                        decorators.pop(k, None)
                else:
                    decorators.update(call["decorators"])
