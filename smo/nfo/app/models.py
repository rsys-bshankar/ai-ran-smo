"""Database tables of NFO: deployment descriptors, deployments, their cloud-resource links and the LCM operation history.

Used by `main.py`; the schema is created by the Alembic revisions in `migrations/`. `NFDeployment` is the only versioned table
(optimistic concurrency, `smo_shared.versioning.Versioned`). The foreign keys between `nf_deployment`, `nf_ocloud_resource` and
`lcm_operation` are real and enforced on Postgres, which decides the delete order in `main._remove_deployment`.
"""

import datetime
import uuid

from sqlalchemy import DateTime, ForeignKey, JSON, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from smo_shared.db import Base
from smo_shared.versioning import Versioned


class NFDeploymentDescriptor(Base):
    """The workload description a deployment is created from. `package_id` refers to Onboarding's application package but is not
    an ORM foreign key (see the comment on the column), and is NULL for a descriptor made for a model runtime.
    """
    __tablename__ = "nf_deployment_descriptor"

    nf_deployment_descriptor_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    # Cross-module reference: enforced by the FK in migrations/001_init.sql, not
    # declared as an ORM ForeignKey — this module runs in its own process, where
    # the other module's table isn't in the metadata and an ORM FK can't resolve
    # (NoReferencedTableError on flush). tests_integration/test_module_isolation.py.
    # Nullable since Wave 2 (AI Platform Service Decomposition): a model
    # runtime's own descriptor (AIMgF's Runtime Lifecycle) has no
    # onboarded ApplicationPackage behind it, unlike an rApp's.
    package_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)  # -> application_package (Onboarding)
    name: Mapped[str] = mapped_column(String, nullable=False)
    required_resource_type_id: Mapped[str | None] = mapped_column(String)
    workload_template: Mapped[dict] = mapped_column(JSON, nullable=False)


class NFDeployment(Versioned, Base):
    """One deployment of a descriptor. `state` holds a `statemachine.DeploymentState` value as text and is changed only through
    `NFO_FSM`. `name` is used by at most one deployment, a rule the Instantiate route checks (no unique constraint backs it:
    `UNIQUE-SELECT`); a descriptor may back any number of deployments (call flow 15b). `cluster_id` is the O-Cloud id from FOCOM. `workload_ref` and `config_secrets` are not written by any
    route in this module. Versioned: a stale write raises and becomes a 409.
    """
    __tablename__ = "nf_deployment"

    nf_deployment_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    nf_deployment_descriptor_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("nf_deployment_descriptor.nf_deployment_descriptor_id"))
    name: Mapped[str] = mapped_column(String, nullable=False)
    cluster_id: Mapped[str] = mapped_column(String, nullable=False)  # degenerate single value, Phase 1
    state: Mapped[str] = mapped_column(String, nullable=False, default="INITIAL")
    workload_ref: Mapped[str | None] = mapped_column(String)
    required_resource_type_id: Mapped[str | None] = mapped_column(String)
    config_secrets: Mapped[dict | None] = mapped_column(JSON)  # reference to secrets store only, never plaintext
    # OI-3-nfo-abnormal: why the deployment is ABNORMAL (the DMS event and its
    # detail); cleared when Heal recovers it.
    abnormal_reason: Mapped[str | None] = mapped_column(String)


class NFOCloudResource(Base):
    """Links a deployment to the O-Cloud resource it uses. One row per deployment is written by Instantiate, with `resource_ref`
    set to the cluster id; finer per-resource detail is not tracked.
    """
    __tablename__ = "nf_ocloud_resource"

    resource_link_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    nf_deployment_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("nf_deployment.nf_deployment_id"), nullable=False)
    resource_ref: Mapped[str] = mapped_column(String, nullable=False)
    vresource_type: Mapped[str] = mapped_column(String, nullable=False, default="COMPUTE")


class LCMOperation(Base):
    """One lifecycle operation of a deployment (INSTANTIATE, SCALE, HEAL, TERMINATE) and its status; `created_at` orders the history."""
    __tablename__ = "lcm_operation"

    operation_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    nf_deployment_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("nf_deployment.nf_deployment_id"))
    operation_type: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False, default="PENDING")
    # the order of a deployment's operation history (migration 0027)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False,
                                                           default=lambda: datetime.datetime.now(datetime.UTC))
