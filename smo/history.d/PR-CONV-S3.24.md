### PR-CONV-S3.24 — The O-RAN WG10 TE&IV R1 API in specs/, its 16 operations in teiv.yaml mapped onto smo-teiv 0.3.0; the extension checked against the TE&IV data model

**The API.** O-RAN.WG10.TS.TE&IV-API.0-R005-v04.00 (supplied by the owner as the PDF: o-ran.org needs a click-through) carries the TE&IV OpenAPI in Annex A
(normative). `specs/oran/teiv/TEIV_API_R005_v04.yaml` is that annex, OpenAPI 3.0.3, `info.version` **1.2.1**, 16 operations under
`/topology-inventory/v1`, extracted, not typed: `specs/oran/teiv/extract_api.py` reads the PDF's own text layer (poppler's `pdftotext -bbox-layout`) and
rebuilds each line from the x position of each word on the annex's 4.80 pt monospace grid (the PDF positions the YAML; it has no space characters), and
fails on any word off the grid. Only extraction artefacts are fixed, by three rules, every application listed by `--report`: A1, 51 lines of page
furniture dropped (the footer, its page number and the header at each of 17 page breaks); A3, 24 blank lines restored from the line spacing (a gap of
two 9.65 pt pitches; the paragraph breaks inside `description: |` blocks are content); A2, 11 lines the PDF wrapped at the page width joined to the line
before (a continuation starts at column 0, where only a top-level key can). The result validates with the bundle's redocly rules (`spec`, as the Contracts
job runs them); redocly's default rules also report that the document declares no security scheme (16) and no licence object, which is the
specification's content. `teiv/tests/test_teiv_api_spec.py` checks the version, the document id, the server and the 16 operations always, and, when the
PDF is at hand (next to the repository, or `TEIV_API_PDF`), that the PDF states `version: 1.2.1` and that the extraction gives the file byte for byte.
Source and version, with the SHA-256 of the PDFs and the file: `specs/README.md`; `docs/STANDARDS.md`'s TE&IV row.

**The mapping onto smo-teiv 0.3.0** (`teiv.yaml` `x-smo-teiv-api`, the spike's `R1_OPERATIONS`; smo-teiv's API is 1.2.0). Compared by method, path,
operationId, parameters, request body and response codes: all 16 exist in smo-teiv with the same method, path, operationId, parameters and request
body. **One to one: 14** (getAllDomains, getTopologyEntityTypes, getTopologyByEntityTypeName, getTopologyById, getAllRelationshipsForEntityId,
getTopologyRelationshipTypes, getRelationshipsByType, getRelationshipById, getSchemas, getSchemaByName, createUserDefinedSchema, getUserDefinedSchemas,
getUserDefinedSchemaByName, deleteUserDefinedSchema). **Differ: 2**, updateClassifier and updateDecorator, which smo-teiv may answer with 409 (not a code
of the standard's). Common to the collection answers: smo-teiv adds paging links (self, first, prev, next, last) and does not declare the standard's
required members. smo-teiv's own extra operations (groups, createSchema, deleteSchema, getEntitiesByDomain, the deprecated /classifiers and /decorators)
stay internal. `teiv.yaml` `$ref`s the twelve paths under `/topology-inventory/v1` of TE&IV's R1 root and lists no pending WG10 service (G9 closed); the
contract-drift baseline of `teiv` goes from 40 to 56 (the 16 operations no route serves until the facade, S3.19).

**The extension and the data model.** TE&IV-DM R005 v06.00 (and TE&IV-GAPUCR R005 v04.00 for reference) was read against smo-teiv 0.3.0's model and
`teiv/model/`: the comparison is ADR 0013 section 11.7. The data model has no NRCellRelation and no neighbour relationship (nor SubNetwork,
CommonBeamformingFunction, NRFreqRelation or SON classes), so the extension stays two TE&IV domain modules (`EXT_RAN`, `EXT_OAM`), a [D] item: ADR 0011
D-24, `teiv.yaml` `x-smo-teiv-domains`. `teiv/model/check_extension.py` holds them to the data model's extension rules (Annex A): no augment or deviation
(so nothing the data model says MUST NOT be augmented is touched), their own namespace and domain, `Top_Grp_Type` and the mapping groupings on every entity
type and relationship, relationships at module level with one instance-identifier aSide and bSide, revision labels, role names unique per type against
smo-teiv's modules. The Contracts job runs it and `pyang --strict` on the extension with smo-teiv 0.3.0's modules (fetched at the pinned commit) on the
search path. **createUserDefinedSchema cannot add entity or relationship types:** TE&IV-CIMI R005 v07.00 section 4.5 makes user-defined data classifiers
and decorators on existing entities and relationships only, the API has no operation that creates a topology schema, and smo-teiv 0.3.0 does the same;
the extension stays built into the images (ADR 0013 section 11.7).

**Not done: the data model's YANG** (o-ran-smo-teiv-ran, -physical, -cloud, -rel-physical-ran, -rel-cloud-ran, -rel-ran-cloud,
-common-yang-types, -common-yang-extensions at v06.00) into `specs/oran/teiv/yang/` with `pyang --strict` in Contracts: their source,
gitlab.o-ran.org/wg10/teiv-dm, is refused by this environment's network (the proxy answers 403), and the modules are not re-typed from the PDF's annexes.
That is PR-CONV-S3.24a, waiting for the owner's download. Note for it: DM v06's annexes carry o-ran-smo-teiv-rel-cloud-ran but no -rel-ran-cloud.
