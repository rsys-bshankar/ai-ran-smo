"""The tables of DME (Data Management and Exposure): producers, the data types they offer, the many-to-many link between them, type subscriptions, data jobs, data offers, the
records a producer ingests and the audit trail of mediated O1 actions.

Used by `main.py` only, and migrated by `migrations/` (the ORM models must match it: `scripts/check_migration_matches_models.py`). `DELIVERY_METHODS`, `SOURCE_DOMAINS` and
`LIFECYCLE_STAGES` are the allowed values the routes check; the columns hold plain strings, so the database does not enforce them. The design record is `HISTORY.md` section 7 (DME against the
real ICS API) and `docs/ARCHITECTURE.md` (DME).
"""

import datetime
import uuid

from sqlalchemy import ARRAY, DateTime, ForeignKey, Integer, JSON, String, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from smo_shared.db import Base

DELIVERY_METHODS = {"PULL_HTTP", "PUSH_HTTP", "STREAMING_KAFKA"}  # R1AP's exact wire values, Foundational LLD section 3.2

# Wave 3 (AI Platform Service Decomposition) — docs/ARCHITECTURE.md (DME).
# A producer's own data domain: whether what it produces is live-RAN data
# or Digital Twin data. Drives the one eligibility rule Phase-1 actually
# needs — a Digital Twin may feed Training/Emulation, never Inference.
SOURCE_DOMAINS = {"LIVE_RAN", "DIGITAL_TWIN"}

# A DataJob's place in the AI/ML lifecycle — what the requested data is
# actually for, independent of which producer/source_domain serves it.
LIFECYCLE_STAGES = {"TRAINING", "TESTING", "EMULATION", "INFERENCE", "CLOSED_LOOP_FEEDBACK"}


class DMEProducer(Base):
    """HISTORY.md §7 — DME vs. the real ICS API: ICS's own real Information
    Producer entity (`producer_registration_info` — `PUT
    /data-producer/v1/info-producers/{infoProducerId}`), previously
    conflated into `DMEType` itself. `producer_id` is the caller's own
    chosen identity (matching ICS's own path-param convention, and this
    build's existing "producer_id is the caller's own identity string"
    pattern — e.g. RAppInstance.oauth_client_id).
    """

    __tablename__ = "dme_producer"

    producer_id: Mapped[str] = mapped_column(String, primary_key=True)
    producer_health_callback_url: Mapped[str] = mapped_column(String, nullable=False)
    job_callback_url: Mapped[str] = mapped_column(String, nullable=False)


class DMEType(Base):
    """A registered data type, identified by (namespace, name, version), which is unique. It carries the JSON schema that a data job's production definition must satisfy
    (`data_production_schema`) and, when the producer declared them, the source domain (LIVE_RAN or DIGITAL_TWIN) and source context. The producers that support the type are
    in `DMEProducerType`, not here.
    """
    __tablename__ = "dme_type"
    __table_args__ = (UniqueConstraint("namespace", "name", "version"),)

    dme_type_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    namespace: Mapped[str] = mapped_column(String, nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)
    version: Mapped[str] = mapped_column(String, nullable=False)
    type_name: Mapped[str] = mapped_column(String, nullable=False)
    data_production_schema: Mapped[dict] = mapped_column(JSON, nullable=False)
    collection_spec: Mapped[dict | None] = mapped_column(JSON)
    # Wave 3: source provenance, docs/ARCHITECTURE.md's DME
    # multi-vendor/multi-Digital-Twin principle. source_context is a
    # flexible dict (vendor/product/release/instance/node/cell) rather
    # than eight forced columns — nothing in this build yet queries most
    # of those fields individually.
    source_domain: Mapped[str | None] = mapped_column(String)
    source_context: Mapped[dict | None] = mapped_column(JSON)
    # SEC-15.10: who registered the type first: the invoker id the gateway vouched for (an rApp's own id, or an SMO module's), else the `producerId` of the request when it did not
    # come through the gateway. Only that caller (or an SMO module / the operator) may change the type's definition later. NULL on a row made before revision 0039: for those the
    # producers linked to the type stand in for it (`main._may_redefine_type`).
    registered_by: Mapped[str | None] = mapped_column(String)

    @property
    def dme_type_id_struct(self) -> dict:
        """Computed at read time — R1AP's actual wire identity (Annex B.4),
        derived from our internal UUID PK. Foundational Platform LLD section 3.1.
        """
        return {"namespace": self.namespace, "name": self.name, "version": self.version}


class DMEProducerType(Base):
    """The real many-to-many relationship ICS's own `producer_registration_info.
    supported_info_types` models — a producer supports zero or more types,
    and (`consumer_information_type.no_of_producers`) a type may be
    supported by zero or more producers. HISTORY.md §7's own closed
    finding: this build's `DMEType` used to conflate identity with its
    single registering producer, making a second producer for the same
    type structurally impossible.
    """

    __tablename__ = "dme_producer_type"

    producer_id: Mapped[str] = mapped_column(String, ForeignKey("dme_producer.producer_id", ondelete="CASCADE"), primary_key=True)
    dme_type_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("dme_type.dme_type_id", ondelete="CASCADE"), primary_key=True)


class DMETypeSubscription(Base):
    """HISTORY.md §5: ICS's own `/info-type-subscription`
    (InfoTypeSubscriptions/ConsumerCallbacks) — a consumer subscribes to
    be notified when any DmeType is registered or removed. Entirely
    absent from this build until now.
    """

    __tablename__ = "dme_type_subscription"

    subscription_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    notification_destination: Mapped[str] = mapped_column(String, nullable=False)
    owner: Mapped[str] = mapped_column(String, nullable=False)


class DMEDeliverySchema(Base):
    """A delivery schema of a type (id, kind and JSON schema). Nothing in this module reads or writes it; the table exists in the database schema and is removed with its type
    (ON DELETE CASCADE).
    """
    __tablename__ = "dme_delivery_schema"

    delivery_schema_id: Mapped[str] = mapped_column(String, primary_key=True)
    dme_type_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("dme_type.dme_type_id", ondelete="CASCADE"))
    schema_type: Mapped[str] = mapped_column(String, nullable=False)
    schema: Mapped[dict] = mapped_column(JSON, nullable=False)


class DataJob(Base):
    """One consumer's request for data of a type: how it is delivered (mode and method), the production definition checked against the type's schema, who asked (`consumer_id`, an rApp id or
    `DME_FRAMEWORK`), its status and the AI/ML lifecycle stage it is for. Deleted with its type (ON DELETE CASCADE) and by the terminate routes.
    """
    __tablename__ = "data_job"

    data_job_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    data_delivery_mode: Mapped[str] = mapped_column(String, nullable=False)
    dme_type_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("dme_type.dme_type_id", ondelete="CASCADE"))  # NEW section 5: matches dme_delivery_schema's own already-cascading FK
    production_job_definition: Mapped[dict | None] = mapped_column(JSON)
    data_delivery_method: Mapped[str] = mapped_column(String, nullable=False)
    delivery_details: Mapped[dict | None] = mapped_column(JSON)
    consumer_id: Mapped[str] = mapped_column(String, nullable=False)  # rAppId, or 'DME_FRAMEWORK' (section 3.7)
    status: Mapped[str] = mapped_column(String, nullable=False, default="PENDING")
    # Wave 3: which AI/ML lifecycle stage this job's data is for — drives
    # the Digital-Twin-excluded-from-inference eligibility check in main.py.
    lifecycle_stage: Mapped[str | None] = mapped_column(String)
    # GUI-9.8 (revision 0037): delivery health. `expected_interval_seconds` is how often the consumer expects data (declared on the job, optional);
    # `last_delivery_at` is when a producer last delivered a record for the job (POST /data-jobs/{id}/records; revision 0037 filled it from
    # data_record); `late_after` is when the job turns LATE, two intervals after the last delivery (or after the job was declared, before the first
    # one), kept as a column so `GET /data-jobs?late=` is a plain comparison in SQL. All null for a job that declares no interval.
    expected_interval_seconds: Mapped[int | None] = mapped_column(Integer)
    last_delivery_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True))
    late_after: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True))


class DataOffer(Base):
    """A producer's offer to deliver data of a type: the delivery methods offered, the one the framework committed to (the first), where to announce availability and where to tell the
    producer the offer ended. Deleted with its type (ON DELETE CASCADE) and by the terminate route.
    """
    __tablename__ = "data_offer"

    offer_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    dme_type_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("dme_type.dme_type_id", ondelete="CASCADE"))  # NEW section 5: matches dme_delivery_schema's own already-cascading FK
    data_delivery_methods_offered: Mapped[list[str]] = mapped_column(ARRAY(String).with_variant(JSON(none_as_null=True), "sqlite"), nullable=False)
    data_delivery_method_committed: Mapped[str | None] = mapped_column(String)
    data_availability_notification_uri: Mapped[str | None] = mapped_column(String)  # REVERSED direction, section 3.5
    data_offer_termination_notification_uri: Mapped[str] = mapped_column(String, nullable=False)


class DataRecord(Base):
    """Wave 3: DME's real data-plane store (docs/ARCHITECTURE.md (DME)).
    A producer ingests an actual payload against its own DataJob
    (POST /data-jobs/{id}/records); a consumer — rApp or MDAF, no
    distinction at this layer — fetches it back
    (GET /data-jobs/{id}/records). Previously DME only ever brokered
    job/offer metadata and left real data movement to the negotiated
    delivery method happening entirely outside DME; this is the pull
    case made real, not a full data-lake redesign.
    """

    __tablename__ = "data_record"

    record_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    data_job_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("data_job.data_job_id", ondelete="CASCADE"))
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    produced_at: Mapped[datetime.datetime] = mapped_column(default=lambda: datetime.datetime.now(datetime.UTC))


class DmeActionRecord(Base):
    """Wave 3: DME's O1 action-mediation audit trail
    (docs/ARCHITECTURE.md (DME)). Records what an rApp's AI/ML
    decision asked for; the forwarded ran-nf-oam WriteConfigJob (own
    job_id stored here as forwarded_job_id) is the record of what
    NETCONF actually did — this table is deliberately not a duplicate
    of that one.
    """

    __tablename__ = "dme_action_record"

    action_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    requested_by: Mapped[str] = mapped_column(String, nullable=False)
    managed_element_ref: Mapped[str] = mapped_column(String, nullable=False)
    class_name: Mapped[str | None] = mapped_column(String)  # real ProvMnS IOC name where known, e.g. GNBDUFunction/NRCellDU
    changes: Mapped[list] = mapped_column(JSON, nullable=False)
    source_context: Mapped[dict | None] = mapped_column(JSON)
    forwarded_job_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    status: Mapped[str] = mapped_column(String, nullable=False, default="FORWARDED")
    # Wave 10.1 (W10-23): the X-Correlation-ID of the request that caused it
    correlation_id: Mapped[str | None] = mapped_column(String)
    created_at: Mapped[datetime.datetime] = mapped_column(default=lambda: datetime.datetime.now(datetime.UTC))
