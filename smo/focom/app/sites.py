"""The O2-IMS site model: Location, OCloudSite and ResourcePool create / list / get / delete routes (SA-FOCOM-2).

What it is: the routes `/locations`, `/o-cloud-sites` and `POST` / `DELETE /resource-pools` (the read routes of the pools are in `main.py`), mounted by
`main.py` through `router`. Design record: `focom/README.md` (1.2, 2.4, 2.8).

Owns: the referential rules of the three kinds: a site names an existing location, a pool names an existing site, ids are unique, and a parent that still
has children cannot be deleted. All of these answer 422 `SCHEMA_VALIDATION_FAILED`; an unknown id on get answers 404 `NRM_OBJECT_NOT_FOUND`.
Does not own: which pool `POST /resources/provision` uses (its body's `resourcePoolId`, else `pool-0`; in `main.py`), or any authorization (R1 Termination decides who may call).

Before editing: every object created here gets the single Phase 1 `oCloudId` (`PHASE1_CLUSTER_ID`); there is no way to create a second O-Cloud. The
seeded `loc-0`, `site-0` and `pool-0` are ordinary rows and can be deleted once empty, but `ensure_phase1_topology` seeds them again on the next read.
"""

import uuid

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.orm import Session

from smo_shared.db import get_session
from smo_shared.errors import FrameworkError, framework_error
from smo_shared.pagination import PageLimit, PageOffset, paginate

from .common import PHASE1_CLUSTER_ID, AttributeValuePair, ensure_phase1_topology, location_view, pool_view, site_view
from .models import Location, OCloudSite, Resource, ResourcePool

router = APIRouter()


# Body of POST /locations. `globalLocationId` is optional (a UUID is generated); unknown fields are refused (422). `coordinate` and `address` are free strings.
class LocationBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    globalLocationId: str | None = None
    name: str
    description: str = ""
    coordinate: str | None = None
    address: str | None = None
    extensions: list[AttributeValuePair] = []


# Body of POST /o-cloud-sites. `locationId` must name an existing location; `oCloudSiteId` is optional (a UUID is generated); unknown fields are refused.
class SiteBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    oCloudSiteId: str | None = None
    locationId: str
    name: str
    description: str = ""
    extensions: list[AttributeValuePair] = []


# Body of POST /resource-pools. `oCloudSiteId` must name an existing site; `resourcePoolId` is optional (a UUID is generated); unknown fields are refused.
class PoolBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    resourcePoolId: str | None = None
    name: str
    description: str = ""
    oCloudSiteId: str
    extensions: list[AttributeValuePair] = []


def _missing(kind: str, object_id: str):
    """Returns the 404 `NRM_OBJECT_NOT_FOUND` problem for an unknown `kind` / `object_id` (the caller raises it)."""
    return framework_error(FrameworkError.NRM_OBJECT_NOT_FOUND, detail=f"no such {kind} {object_id}")


def _conflict(detail: str):
    """Returns the 422 `SCHEMA_VALIDATION_FAILED` problem used for every referential or uniqueness refusal in this file, despite the name (the caller raises it)."""
    return framework_error(FrameworkError.SCHEMA_VALIDATION_FAILED, detail=detail)


@router.post("/locations", status_code=201)
def create_location(body: LocationBody, db: Session = Depends(get_session)):
    # Route notes (kept out of the docstring because FastAPI publishes it): seeds the topology first, then 422 if `globalLocationId` already exists. The
    # location belongs to the Phase 1 O-Cloud. One commit; no notification is sent.
    ensure_phase1_topology(db)
    location_id = body.globalLocationId or str(uuid.uuid4())
    if db.get(Location, location_id) is not None:
        raise _conflict(f"location {location_id} already exists")
    loc = Location(global_location_id=location_id, name=body.name, description=body.description, o_cloud_id=PHASE1_CLUSTER_ID,
                   coordinate=body.coordinate, address=body.address, extensions=[e.model_dump() for e in body.extensions])
    db.add(loc)
    db.commit()
    return location_view(db, loc)


@router.get("/locations")
def list_locations(limit: int = PageLimit, offset: int = PageOffset, db: Session = Depends(get_session)):
    # Route notes: seeds the topology first, so the first call on an empty database returns `loc-0`. Paginated.
    ensure_phase1_topology(db)
    page = paginate(db, select(Location), limit, offset)
    return {**page, "items": [location_view(db, loc) for loc in page["items"]]}


@router.get("/locations/{location_id}")
def get_location(location_id: str, db: Session = Depends(get_session)):
    # Route notes: 404 `NRM_OBJECT_NOT_FOUND` for an unknown id.
    ensure_phase1_topology(db)
    loc = db.get(Location, location_id)
    if loc is None:
        raise _missing("location", location_id)
    return location_view(db, loc)


@router.delete("/locations/{location_id}", status_code=204)
def delete_location(location_id: str, db: Session = Depends(get_session)):
    # Route notes: idempotent, 204 for an unknown id. 422 when a site still names the location. No seeding happens here, so deleting before any read finds nothing.
    loc = db.get(Location, location_id)
    if loc is not None:
        if db.scalars(select(OCloudSite).where(OCloudSite.location_id == location_id)).first() is not None:
            raise _conflict(f"location {location_id} still has O-Cloud sites")
        db.delete(loc)
        db.commit()


@router.post("/o-cloud-sites", status_code=201)
def create_site(body: SiteBody, db: Session = Depends(get_session)):
    # Route notes: 422 if the location does not exist or `oCloudSiteId` already exists (the location is checked first). The check and the insert are two
    # statements in one transaction without a lock, so two concurrent creates of one id can both pass the check and one then fails at the commit (500).
    ensure_phase1_topology(db)
    if db.get(Location, body.locationId) is None:
        raise _conflict(f"unknown location {body.locationId}")
    site_id = body.oCloudSiteId or str(uuid.uuid4())
    if db.get(OCloudSite, site_id) is not None:
        raise _conflict(f"O-Cloud site {site_id} already exists")
    site = OCloudSite(o_cloud_site_id=site_id, location_id=body.locationId, name=body.name, description=body.description,
                      o_cloud_id=PHASE1_CLUSTER_ID, extensions=[e.model_dump() for e in body.extensions])
    db.add(site)
    db.commit()
    return site_view(db, site)


@router.get("/o-cloud-sites")
def list_sites(limit: int = PageLimit, offset: int = PageOffset, db: Session = Depends(get_session)):
    # Route notes: seeds the topology first. Paginated; each site carries its pools inline (`site_view`).
    ensure_phase1_topology(db)
    page = paginate(db, select(OCloudSite), limit, offset)
    return {**page, "items": [site_view(db, s) for s in page["items"]]}


@router.get("/o-cloud-sites/{site_id}")
def get_site(site_id: str, db: Session = Depends(get_session)):
    # Route notes: 404 `NRM_OBJECT_NOT_FOUND` for an unknown id.
    ensure_phase1_topology(db)
    site = db.get(OCloudSite, site_id)
    if site is None:
        raise _missing("O-Cloud site", site_id)
    return site_view(db, site)


@router.delete("/o-cloud-sites/{site_id}", status_code=204)
def delete_site(site_id: str, db: Session = Depends(get_session)):
    # Route notes: idempotent, 204 for an unknown id. 422 when a resource pool still names the site (so `site-0` cannot be deleted while `pool-0` exists).
    site = db.get(OCloudSite, site_id)
    if site is not None:
        if db.scalars(select(ResourcePool).where(ResourcePool.o_cloud_site_id == site_id)).first() is not None:
            raise _conflict(f"O-Cloud site {site_id} still has resource pools")
        db.delete(site)
        db.commit()


@router.post("/resource-pools", status_code=201)
def create_resource_pool(body: PoolBody, db: Session = Depends(get_session)):
    # Route notes: 422 if the site does not exist or `resourcePoolId` already exists (the site is checked first). Creating a pool does not make
    # `POST /resources/provision` use it: provisioning always places resources in `pool-0`.
    ensure_phase1_topology(db)
    if db.get(OCloudSite, body.oCloudSiteId) is None:
        raise _conflict(f"unknown O-Cloud site {body.oCloudSiteId}")
    pool_id = body.resourcePoolId or str(uuid.uuid4())
    if db.get(ResourcePool, pool_id) is not None:
        raise _conflict(f"resource pool {pool_id} already exists")
    pool = ResourcePool(resource_pool_id=pool_id, name=body.name, description=body.description, o_cloud_id=PHASE1_CLUSTER_ID,
                        o_cloud_site_id=body.oCloudSiteId, extensions=[e.model_dump() for e in body.extensions])
    db.add(pool)
    db.commit()
    return pool_view(db, pool)


@router.delete("/resource-pools/{resource_pool_id}", status_code=204)
def delete_resource_pool(resource_pool_id: str, db: Session = Depends(get_session)):
    # Route notes: idempotent, 204 for an unknown id. 422 when a resource still sits in the pool. `pool-0` itself can be deleted once empty; the next read seeds it again.
    pool = db.get(ResourcePool, resource_pool_id)
    if pool is not None:
        if db.scalars(select(Resource).where(Resource.resource_pool_id == resource_pool_id)).first() is not None:
            raise _conflict(f"resource pool {resource_pool_id} still has resources")
        db.delete(pool)
        db.commit()
