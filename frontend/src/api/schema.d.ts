/**
 * Generated from docs/contracts/openapi.yaml (C4) by `npm run gen:api`. Do not edit by hand.
 * The contract is the only source of request and response shapes; CI fails when this file is stale.
 */

export interface paths {
    "/credentials": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** List TOS access keys (never returns secrets) */
        get: operations["listCredentials"];
        put?: never;
        /**
         * Save an access key; verifies identity only (D30, doc 08 §4)
         * @description Saving never fails because verification failed: the key is stored and marked
         *     `failed` or `unverified`. Read and write permissions are checked against concrete
         *     buckets when a task starts.
         */
        post: operations["createCredential"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/credentials/{id}": {
        parameters: {
            query?: never;
            header?: never;
            path: {
                id: components["parameters"]["PathId"];
            };
            cookie?: never;
        };
        get?: never;
        /** Update; a secret left empty stays unchanged */
        put: operations["updateCredential"];
        post?: never;
        /**
         * Delete (409 credential_in_use while a non-terminal task uses it)
         * @description Referenced only by finished tasks: requires `confirm=true`. Those tasks keep their
         *     reports, which then say the key is gone; rebind-credentials gives them another one.
         */
        delete: operations["deleteCredential"];
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/credentials/{id}/verify": {
        parameters: {
            query?: never;
            header?: never;
            path: {
                id: components["parameters"]["PathId"];
            };
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** Verify identity again */
        post: operations["verifyCredential"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/vlm-backends": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** List backends with their models */
        get: operations["listVlmBackends"];
        put?: never;
        /**
         * Add a backend with its API key; then try GET {endpoint}/models once (D8)
         * @description Not listing any models is not an error; the user adds model IDs by hand.
         */
        post: operations["createVlmBackend"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/vlm-backends/{id}": {
        parameters: {
            query?: never;
            header?: never;
            path: {
                id: components["parameters"]["PathId"];
            };
            cookie?: never;
        };
        get?: never;
        /** Update; an API key left empty stays unchanged */
        put: operations["updateVlmBackend"];
        post?: never;
        /**
         * Delete with its models and key (409 backend_in_use while a non-terminal task uses it)
         * @description An unfinished task using it answers 409 in_use. When only finished (or deleted) tasks
         *     use it, the 409 carries `error.details.confirm_required: true`; repeating the call
         *     with `confirm=true` deletes it and those tasks lose the reference.
         */
        delete: operations["deleteVlmBackend"];
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/vlm-backends/{id}/verify": {
        parameters: {
            query?: never;
            header?: never;
            path: {
                id: components["parameters"]["PathId"];
            };
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** Probe the endpoint (GET /models, else a minimal chat call) */
        post: operations["verifyVlmBackend"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/vlm-backends/{id}/refresh-models": {
        parameters: {
            query?: never;
            header?: never;
            path: {
                id: components["parameters"]["PathId"];
            };
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** List models again; an empty result means "enter them by hand" */
        post: operations["refreshVlmModels"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/vlm-backends/{id}/models": {
        parameters: {
            query?: never;
            header?: never;
            path: {
                id: components["parameters"]["PathId"];
            };
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** Add a model ID or inference endpoint ID (ep-...), checked with one minimal call */
        post: operations["addVlmModel"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/vlm-backends/{id}/models/{model_id}": {
        parameters: {
            query?: never;
            header?: never;
            path: {
                id: components["parameters"]["PathId"];
                model_id: string;
            };
            cookie?: never;
        };
        get?: never;
        put?: never;
        post?: never;
        /** Remove a model (409 backend_in_use while a non-terminal task uses it) */
        delete: operations["deleteVlmModel"];
        options?: never;
        head?: never;
        /** Set reasoning effort and parallelism */
        patch: operations["updateVlmModel"];
        trace?: never;
    };
    "/modules": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** The module registry (C1); the frontend's module list comes only from here */
        get: operations["getModules"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/overview": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** The overview page in one call - what needs attention, what runs, the chosen period (D36) */
        get: operations["getOverview"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/datasets": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Registered datasets, page-number pagination, newest first (D36) */
        get: operations["listDatasets"];
        put?: never;
        /**
         * Register a dataset - preflight plus the full file listing, both fingerprints kept (D36)
         * @description Registering the same source + address + region again returns the existing registration
         *     with 200 instead of creating a second one.
         */
        post: operations["createDataset"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/datasets/browse": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** List datasets under a private TOS prefix or in the HuggingFace cache bucket, to pick one to register */
        get: operations["browseDatasets"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/datasets/episodes": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /**
         * Episodes for the preview grid; metadata only, no decoding on the server (doc 03 §10)
         * @description Give `dataset_id`, or `source` + `uri` (+ `region`, `credential`).
         */
        get: operations["listDatasetEpisodes"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/datasets/{id}": {
        parameters: {
            query?: never;
            header?: never;
            path: {
                id: components["parameters"]["PathId"];
            };
            cookie?: never;
        };
        /** One registration with its preflight result, recent fingerprint checks and tasks */
        get: operations["getDataset"];
        put?: never;
        post?: never;
        /** Remove the registration; the data on TOS is untouched. In use by an unfinished task -> 409 dataset_in_use */
        delete: operations["deleteDataset"];
        options?: never;
        head?: never;
        /** Rename, or change the note */
        patch: operations["updateDataset"];
        trace?: never;
    };
    "/datasets/{id}/recheck": {
        parameters: {
            query?: never;
            header?: never;
            path: {
                id: components["parameters"]["PathId"];
            };
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** Recompute both fingerprints and compare, nothing else changes; a difference sets check_state=changed */
        post: operations["recheckDataset"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/datasets/{id}/repreflight": {
        parameters: {
            query?: never;
            header?: never;
            path: {
                id: components["parameters"]["PathId"];
            };
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** Run preflight again and keep the new result and fingerprints; check_state returns to ok */
        post: operations["repreflightDataset"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/datasets/{id}/sign": {
        parameters: {
            query?: never;
            header?: never;
            path: {
                id: components["parameters"]["PathId"];
            };
            cookie?: never;
        };
        get?: never;
        put?: never;
        /**
         * Presigned S3 URLs for the ReRun viewer to read a registered TOS dataset (D55)
         * @description Design doc 15 §2. For `source: tos` registrations only (public and local ones answer
         *     400). Each request is one read: `get` an object by its full key in the bucket, or `list`
         *     one ListObjectsV2 page (`list-type=2`, `prefix`, optional `delimiter`,
         *     `continuation-token`, `max-keys`). Keys and prefixes must start with the dataset's
         *     prefix and a `/`; they are checked, never rewritten, and one bad item fails the whole
         *     request with `details.errors[].field` = `requests.<i>.key|prefix`. The URLs are for
         *     TOS's public S3-compatible endpoint `https://<bucket>.tos-s3-<region>.volces.com`, sign
         *     only the `host` header (a `Range` header does not change them) and are answered with
         *     `Cache-Control: no-store`. A key the registration no longer has: 404 with
         *     `details.reason: credential_missing`; a key that cannot be decrypted: 500 with
         *     `details.reason: secret_unreadable`; a key with a custom endpoint: 400.
         */
        post: operations["signDataset"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/datasets/{id}/viz": {
        parameters: {
            query?: never;
            header?: never;
            path: {
                id: components["parameters"]["PathId"];
            };
            cookie?: never;
        };
        /**
         * The presentation model of a registered dataset for the visualizer (design doc 18 §4.0)
         * @description Cameras and how each one is served, curve groups, annotation sources and the field tree,
         *     read by the format's reader from the registration's metadata (LeRobot `meta/`, an mcap
         *     file's summary and the dataset's confirmed mapping) - never from the samples - and cached
         *     by the metadata fingerprint. A format without a reader in this phase (lance) answers with
         *     `format.reader` null; an mcap dataset whose mapping is not confirmed answers
         *     `mapping.state: none` with no cameras or streams and a warning.
         */
        get: operations["getDatasetViz"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/datasets/{id}/viz/episodes": {
        parameters: {
            query?: never;
            header?: never;
            path: {
                id: components["parameters"]["PathId"];
            };
            cookie?: never;
        };
        /** The episodes of the visualizer's side rail - number, length, task text, and steps when the episode table has them */
        get: operations["listDatasetVizEpisodes"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/datasets/{id}/viz/meta": {
        parameters: {
            query?: never;
            header?: never;
            path: {
                id: components["parameters"]["PathId"];
            };
            cookie?: never;
        };
        /** A metadata file the field tree names (meta/*, README), for its preview; cut at 256 KiB */
        get: operations["getDatasetVizMeta"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/datasets/{id}/episodes/{index}/viz": {
        parameters: {
            query?: never;
            header?: never;
            path: {
                id: components["parameters"]["PathId"];
                /** @description the episode's own index (preflight `episode_indices`) */
                index: components["parameters"]["PathIndex"];
            };
            cookie?: never;
        };
        /**
         * One episode for the player - its clock, cameras with their URLs, task text and annotations
         * @description Presigned URLs expire (`cameras[].expires_at`); ask again to renew them. Opening an episode
         *     starts preparing the cameras the Daemon serves (remux, frame packs, transcoding) in the
         *     background.
         */
        get: operations["getDatasetEpisodeViz"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/datasets/{id}/episodes/{index}/series": {
        parameters: {
            query?: never;
            header?: never;
            path: {
                id: components["parameters"]["PathId"];
                /** @description the episode's own index (preflight `episode_indices`) */
                index: components["parameters"]["PathIndex"];
            };
            cookie?: never;
        };
        /** One curve group of an episode, min / max thinned to at most `points` (design doc 18 §4.3) */
        get: operations["getDatasetEpisodeSeries"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/datasets/{id}/episodes/{index}/cameras/{camera}.mp4": {
        parameters: {
            query?: never;
            header?: never;
            path: {
                id: components["parameters"]["PathId"];
                /** @description the episode's own index (preflight `episode_indices`) */
                index: components["parameters"]["PathIndex"];
                /** @description the camera's `key` in the presentation model */
                camera: components["parameters"]["PathCamera"];
            };
            cookie?: never;
        };
        /** A camera served by the Daemon - a local dataset's file, a Lance table's blob (2.5.0), a fragmented-mp4 remux or an H.264 transcode - with Range */
        get: operations["getDatasetCameraVideo"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/datasets/{id}/episodes/{index}/cameras/{camera}.frames": {
        parameters: {
            query?: never;
            header?: never;
            path: {
                id: components["parameters"]["PathId"];
                /** @description the episode's own index (preflight `episode_indices`) */
                index: components["parameters"]["PathIndex"];
                /** @description the camera's `key` in the presentation model */
                camera: components["parameters"]["PathCamera"];
            };
            cookie?: never;
        };
        /** The frame pack of a camera - the frames' own bytes back to back (JPEG / PNG, or an H.264 / H.265 sample pack, 2.5.0), read by Range with the index */
        get: operations["getDatasetCameraFrames"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/datasets/{id}/episodes/{index}/cameras/{camera}.json": {
        parameters: {
            query?: never;
            header?: never;
            path: {
                id: components["parameters"]["PathId"];
                /** @description the episode's own index (preflight `episode_indices`) */
                index: components["parameters"]["PathIndex"];
                /** @description the camera's `key` in the presentation model */
                camera: components["parameters"]["PathCamera"];
            };
            cookie?: never;
        };
        /** The index of a camera's frame pack - time, offset and size of every frame (and, for a sample pack, its keyframes and codec string) */
        get: operations["getDatasetCameraFrameIndex"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/datasets/{id}/mapping": {
        parameters: {
            query?: never;
            header?: never;
            path: {
                id: components["parameters"]["PathId"];
            };
            cookie?: never;
        };
        /** The dataset's mcap field mapping (C7) and the check reader's mapping derived from it */
        get: operations["getDatasetMapping"];
        /**
         * Confirm a new version of the mapping (validated against C7 and the dataset's topics)
         * @description Only for mcap datasets - including one the checks could not read with the site's default
         *     topics (its `format` is `unsupported`): its registration's preflight is then taken again
         *     with the new mapping, so it may become `mcap`. Tasks started before keep the version frozen
         *     into their run.json (D62); tasks started after read this one. A mapping that names topics
         *     the dataset does not have, pairs series that are not of opposite roles or names a frame
         *     reference that is not a camera or series fails with `validation_failed` and
         *     `details.errors` per item. `warnings` say what the check reader will not read in it
         *     (`checks_gap`: protobuf repeated fields picked by a path, ROS 2 nested messages, H.265
         *     cameras; design doc 18 §6.2) and when the checks still cannot read the dataset
         *     (`checks_unreadable`).
         */
        put: operations["putDatasetMapping"];
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/datasets/{id}/annotations": {
        parameters: {
            query?: never;
            header?: never;
            path: {
                id: components["parameters"]["PathId"];
            };
            cookie?: never;
        };
        get?: never;
        /** Attach an external annotation file (a `viz_annotations` upload) to the dataset, replace it or remove it (null) */
        put: operations["putDatasetAnnotations"];
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/preflight": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** Preflight a dataset (metadata only, seconds); the result is kept 30 minutes */
        post: operations["preflight"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/deliveries/probe": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** Write and delete one object under the delivery directory with the given key */
        post: operations["probeDelivery"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/uploads": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /**
         * Store the input file of a module parameter after validating it (registry 1.5)
         * @description The body is the file itself, sent as JSON like every write: a trajectory.json as it is (kind eef_trajectory), or the seed rows as a JSON array (kind eef_observation_seeds; the console turns a .jsonl file into that array). Kind viz_annotations also takes a zip of per-episode JSON files named by episode index, sent as application/zip (still not a CORS-safelisted type, so cross-site writes stay refused). Up to 64 MiB. An invalid file is not stored: 400 validation_failed with details.errors, each located as precisely as the file allows (field = JSON path, plus sample_id / episode_index / frame_index / camera_id / point_id). The dataset is not known yet, so media are checked when a task uses the file.
         */
        post: operations["createUpload"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/uploads/{upload_id}": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** An upload's metadata and validation summary (the caller's own uploads only) */
        get: operations["getUpload"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/viz/mcap-probe": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /**
         * Probe an mcap dataset and draft its mapping (design doc 18 §6.4) - before registering it or after
         * @description Reads the summary section of one episode file (the first, or `file`) and the first message
         *     of every topic with ranged reads: schema and encodings, rate, count, picture size and
         *     codec, numeric fields. The draft comes from `template` when given, else from the best
         *     matching template (site templates and the built-ins, coverage >= 80 %), else from
         *     builtin:foxglove / builtin:ros2 by the message encoding. A dataset whose files disagree on
         *     their topics gets a warning.
         */
        post: operations["probeMcap"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/viz/templates": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Mapping templates - the built-ins first, then the site's own, newest first */
        get: operations["listVizTemplates"];
        put?: never;
        /** Save a mapping as a site template (另存为模版); the name must be unique (409 name_taken) */
        post: operations["createVizTemplate"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/viz/templates/{template_id}": {
        parameters: {
            query?: never;
            header?: never;
            path: {
                template_id: components["schemas"]["VizTemplateId"];
            };
            cookie?: never;
        };
        get?: never;
        put?: never;
        post?: never;
        /** Delete a site template (built-ins cannot be deleted); datasets keep the mappings drafted from it */
        delete: operations["deleteVizTemplate"];
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/tasks": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Task list, page-number pagination (D21), newest first */
        get: operations["listTasks"];
        put?: never;
        /**
         * Create a task - the requirement's run_modules() (D5)
         * @description The only way to submit work. The Daemon always plans internally; callers may only give
         *     upper bounds (D31). With `start_now` the three pre-start checks run first (D30): input
         *     readable, delivery writable, VLM callable; any failure returns `precheck_failed` with
         *     a per-check reason and nothing starts.
         */
        post: operations["createTask"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/tasks/batch": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** One configuration, several datasets, one task each (deep link with many datasets) */
        post: operations["createTasksBatch"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/tasks/{id}": {
        parameters: {
            query?: never;
            header?: never;
            path: {
                id: components["parameters"]["PathId"];
            };
            cookie?: never;
        };
        get: operations["getTask"];
        put?: never;
        post?: never;
        /** Soft-delete the platform record; TOS is untouched (D28); created or terminal only */
        delete: operations["deleteTask"];
        options?: never;
        head?: never;
        /**
         * created - any field; after start - only name and note (D20)
         * @description Other fields after start return task_state_conflict. Concurrent edits are guarded by If-Match.
         */
        patch: operations["updateTask"];
        trace?: never;
    };
    "/tasks/{id}/restore": {
        parameters: {
            query?: never;
            header?: never;
            path: {
                id: components["parameters"]["PathId"];
            };
            cookie?: never;
        };
        get?: never;
        put?: never;
        post: operations["restoreTask"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/tasks/{id}/purge-artifacts": {
        parameters: {
            query?: never;
            header?: never;
            path: {
                id: components["parameters"]["PathId"];
            };
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** Delete this task's run directory on TOS; the exact path must be echoed back (D28) */
        post: operations["purgeTaskArtifacts"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/tasks/{id}/rebind-credentials": {
        parameters: {
            query?: never;
            header?: never;
            path: {
                id: components["parameters"]["PathId"];
            };
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** Give a finished task a new access key after the old one was deleted */
        post: operations["rebindTaskCredentials"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/tasks/{id}/actions/{action}": {
        parameters: {
            query?: never;
            header?: never;
            path: {
                id: components["parameters"]["PathId"];
                action: "start" | "pause" | "resume" | "stop";
            };
            cookie?: never;
        };
        get?: never;
        put?: never;
        /**
         * start / pause / resume / stop; illegal transitions return 409 task_state_conflict
         * @description `start` first re-checks the dataset fingerprints against its registration (D37). On a
         *     mismatch nothing starts: 409 `source_changed` whose `error.details` is a `SourceChange`;
         *     after the user confirms, call `POST /tasks/{id}/repreflight`.
         */
        post: operations["taskAction"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/tasks/{id}/repreflight": {
        parameters: {
            query?: never;
            header?: never;
            path: {
                id: components["parameters"]["PathId"];
            };
            cookie?: never;
        };
        get?: never;
        put?: never;
        /**
         * After a source_changed at start, run preflight again and start if the task still fits (D37)
         * @description Refreshes the dataset registration. Compatible (selected modules still available, explicit
         *     episodes still in range, needed inputs present) -> the task starts as with `start`; otherwise
         *     it stays `created` and `incompatibilities` says what to change in the form.
         */
        post: operations["repreflightTask"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/tasks/{id}/retry": {
        parameters: {
            query?: never;
            header?: never;
            path: {
                id: components["parameters"]["PathId"];
            };
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** Re-run only the error episodes, from the stage they failed in (D25, D33) */
        post: operations["retryTask"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/tasks/{id}/continue": {
        parameters: {
            query?: never;
            header?: never;
            path: {
                id: components["parameters"]["PathId"];
            };
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** Resume a stopped or failed task from its checkpoints (not after source_changed) */
        post: operations["continueTask"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/tasks/{id}/reexport": {
        parameters: {
            query?: never;
            header?: never;
            path: {
                id: components["parameters"]["PathId"];
            };
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** Incremental re-export of the delivered dataset (also the first export when export=false) */
        post: operations["reexportTask"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/tasks/{id}/subtasks": {
        parameters: {
            query?: never;
            header?: never;
            path: {
                id: components["parameters"]["PathId"];
            };
            cookie?: never;
        };
        get: operations["listSubtasks"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/tasks/{id}/timeline": {
        parameters: {
            query?: never;
            header?: never;
            path: {
                id: components["parameters"]["PathId"];
            };
            cookie?: never;
        };
        /** Execution timeline - main run, subtasks, pauses and resumes, result revisions */
        get: operations["getTaskTimeline"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/tasks/{id}/plan": {
        parameters: {
            query?: never;
            header?: never;
            path: {
                id: components["parameters"]["PathId"];
            };
            cookie?: never;
        };
        /** The execution plan, read only (404 before the task starts) */
        get: operations["getTaskPlan"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/tasks/{id}/logs": {
        parameters: {
            query?: never;
            header?: never;
            path: {
                id: components["parameters"]["PathId"];
            };
            cookie?: never;
        };
        /**
         * Full logs, cursor paginated; SSE only carries what is happening now
         * @description Newest first; the next page goes back in time. Without `subtask` the main run and every subtask are listed, `subtask=` (empty) keeps the main run only, `subtask=<id>` that subtask only.
         */
        get: operations["getTaskLogs"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/tasks/{id}/usage": {
        parameters: {
            query?: never;
            header?: never;
            path: {
                id: components["parameters"]["PathId"];
            };
            cookie?: never;
        };
        /** Token usage by module, call kind, model and subtask; totals use the actual ledger only */
        get: operations["getTaskUsage"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/tasks/{id}/report": {
        parameters: {
            query?: {
                /** @description result revision; omitted = the current one (task.result_rev) */
                rev?: components["parameters"]["Rev"];
            };
            header?: never;
            path: {
                id: components["parameters"]["PathId"];
            };
            cookie?: never;
        };
        /** Report of the current (or given) result revision */
        get: operations["getReport"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/tasks/{id}/report/tables/{table}": {
        parameters: {
            query?: {
                /** @description result revision; omitted = the current one (task.result_rev) */
                rev?: components["parameters"]["Rev"];
            };
            header?: never;
            path: {
                id: components["parameters"]["PathId"];
                /** @description a table id declared in the module registry */
                table: string;
            };
            cookie?: never;
        };
        /**
         * One slice of a detail table, read from Parquet by the Daemon (doc 03 §6)
         * @description The cursor carries the revision; if the revision changed since the cursor was issued the
         *     server returns result_changed and the client starts again from the first page.
         */
        get: operations["getReportTable"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/tasks/{id}/episodes": {
        parameters: {
            query?: {
                /** @description result revision; omitted = the current one (task.result_rev) */
                rev?: components["parameters"]["Rev"];
            };
            header?: never;
            path: {
                id: components["parameters"]["PathId"];
            };
            cookie?: never;
        };
        /**
         * The episodes of a result revision, for the report's Episode tab (F6.2)
         * @description Every episode of the three final lists in episode order: its list, the modules that put
         *     it there, and whether a person still has to answer something about it (on the current
         *     revision the adjudication cards still to decide - pending or unsure; on a history
         *     revision the questions that revision asked). `q` is an episode number matched as a
         *     substring of the index; a leading `ep` and leading zeros are dropped, so `12`, `ep12`,
         *     `ep 12` and `ep000012` all find ep 12. The cursor carries the revision and is bound to
         *     the filters (another filter set is 400 validation_failed, another revision 409
         *     result_changed). `total` counts the filtered set, `counts` the whole revision. On a findings
         *     revision (C2 2.0) `level` and `item` keep the episodes with a finding of that level / that
         *     taxonomy item (2.3.0); a revision without them (the funnel's) matches none.
         */
        get: operations["listTaskEpisodes"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/tasks/{id}/episodes/{index}": {
        parameters: {
            query?: {
                /** @description result revision; omitted = the current one (task.result_rev) */
                rev?: components["parameters"]["Rev"];
            };
            header?: never;
            path: {
                id: components["parameters"]["PathId"];
                index: number;
            };
            cookie?: never;
        };
        /** One episode across all modules (the v1 trajectory page) */
        get: operations["getEpisode"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/tasks/{id}/pipeline/episodes": {
        parameters: {
            query?: never;
            header?: never;
            path: {
                id: components["parameters"]["PathId"];
            };
            cookie?: never;
        };
        /** Recent per-episode funnel progress, available while the task runs */
        get: operations["listPipelineEpisodes"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/tasks/{id}/pipeline/episodes/{index}": {
        parameters: {
            query?: never;
            header?: never;
            path: {
                id: components["parameters"]["PathId"];
                index: number;
            };
            cookie?: never;
        };
        /** Immediate funnel result and module records of one episode */
        get: operations["getPipelineEpisode"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/tasks/{id}/episodes/{index}/sync-curves": {
        parameters: {
            query?: {
                /** @description result revision; omitted = the current one (task.result_rev) */
                rev?: components["parameters"]["Rev"];
            };
            header?: never;
            path: {
                id: components["parameters"]["PathId"];
                index: number;
            };
            cookie?: never;
        };
        /**
         * One episode's video-action sync curves (F6.2)
         * @description Per camera: picture motion (optical-flow energy) and arm motion (joint speed) over time,
         *     the cross-correlation of the two within the check's scan window, and the check's reading
         *     on that curve. Values are raw; the page normalizes them to compare shapes. At most 600
         *     points per series. 404 not_found with details.reason module_not_run (the sync check was not
         *     selected), no_record (the episode never reached it, or it failed there) or no_curves
         *     (nothing was kept: by default only episodes worth a look keep their curves).
         */
        get: operations["getEpisodeSyncCurves"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/tasks/{id}/viz": {
        parameters: {
            query?: never;
            header?: never;
            path: {
                id: components["parameters"]["PathId"];
            };
            cookie?: never;
        };
        /**
         * The presentation model of a task's input as the task saw it (the mini player, design doc 18 §2)
         * @description Read like `getDatasetViz`, but from the task's frozen input (D27): its address and input
         *     key, `preflight.json`, `source_manifest.json` and the mcap mapping frozen into run.json.
         *     A deleted or changed registration does not change it; `dataset_id` is the task's
         *     registration while it exists.
         */
        get: operations["getTaskViz"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/tasks/{id}/episodes/{index}/viz": {
        parameters: {
            query?: never;
            header?: never;
            path: {
                id: components["parameters"]["PathId"];
                /** @description the episode's own index (preflight `episode_indices`) */
                index: components["parameters"]["PathIndex"];
            };
            cookie?: never;
        };
        /** One episode of the task's input for the mini player, with the check clock to place findings */
        get: operations["getTaskEpisodeViz"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/tasks/{id}/episodes/{index}/series": {
        parameters: {
            query?: never;
            header?: never;
            path: {
                id: components["parameters"]["PathId"];
                /** @description the episode's own index (preflight `episode_indices`) */
                index: components["parameters"]["PathIndex"];
            };
            cookie?: never;
        };
        /** One curve group of an episode of the task's input (as getDatasetEpisodeSeries) */
        get: operations["getTaskEpisodeSeries"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/tasks/{id}/episodes/{index}/cameras/{camera}.mp4": {
        parameters: {
            query?: never;
            header?: never;
            path: {
                id: components["parameters"]["PathId"];
                /** @description the episode's own index (preflight `episode_indices`) */
                index: components["parameters"]["PathIndex"];
                /** @description the camera's `key` in the presentation model */
                camera: components["parameters"]["PathCamera"];
            };
            cookie?: never;
        };
        /**
         * A camera of the task's input served by the Daemon (as getDatasetCameraVideo)
         * @description Also what `GET /media/sign` resolves an mcap episode's virtual camera path to (the
         *     episode view's `videos`): without `transcode`, a JPEG camera is muxed into an MJPEG mp4 in
         *     memory as before 2.4.0.
         */
        get: operations["getTaskCameraVideo"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/tasks/{id}/episodes/{index}/cameras/{camera}.frames": {
        parameters: {
            query?: never;
            header?: never;
            path: {
                id: components["parameters"]["PathId"];
                /** @description the episode's own index (preflight `episode_indices`) */
                index: components["parameters"]["PathIndex"];
                /** @description the camera's `key` in the presentation model */
                camera: components["parameters"]["PathCamera"];
            };
            cookie?: never;
        };
        /** The frame pack of a camera of the task's input (as getDatasetCameraFrames) */
        get: operations["getTaskCameraFrames"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/tasks/{id}/episodes/{index}/cameras/{camera}.json": {
        parameters: {
            query?: never;
            header?: never;
            path: {
                id: components["parameters"]["PathId"];
                /** @description the episode's own index (preflight `episode_indices`) */
                index: components["parameters"]["PathIndex"];
                /** @description the camera's `key` in the presentation model */
                camera: components["parameters"]["PathCamera"];
            };
            cookie?: never;
        };
        /** The index of a camera's frame pack (as getDatasetCameraFrameIndex) */
        get: operations["getTaskCameraFrameIndex"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/tasks/{id}/perf": {
        parameters: {
            query?: {
                /** @description result revision; omitted = the current one (task.result_rev) */
                rev?: components["parameters"]["Rev"];
            };
            header?: never;
            path: {
                id: components["parameters"]["PathId"];
            };
            cookie?: never;
        };
        /** Performance profile - all calls, main run only, or one subtask */
        get: operations["getPerf"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/tasks/{id}/adjudication": {
        parameters: {
            query?: never;
            header?: never;
            path: {
                id: components["parameters"]["PathId"];
            };
            cookie?: never;
        };
        /** The adjudication queue, one card per episode, grouped by source module */
        get: operations["listAdjudication"];
        put?: never;
        /** Record decisions (append only, last one wins, nothing is executed) */
        post: operations["submitAdjudication"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/tasks/{id}/adjudication/apply": {
        parameters: {
            query?: never;
            header?: never;
            path: {
                id: components["parameters"]["PathId"];
            };
            cookie?: never;
        };
        get?: never;
        put?: never;
        /**
         * Execute the recorded decisions as a subtask (D10); never exports (D9)
         * @description The body is optional; without one (or with `{}`) relabelled episodes are judged again the
         *     way v1 does (D39). The choice is kept in the subtask's `scope.relabel_rerun`.
         */
        post: operations["applyAdjudication"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/media/sign": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** A presigned TOS URL for a video or evidence frame (D16); browsers fetch it directly */
        get: operations["signMedia"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/events/tasks/{id}": {
        parameters: {
            query?: never;
            header?: never;
            path: {
                id: components["parameters"]["PathId"];
            };
            cookie?: never;
        };
        /**
         * Server-sent events for one task (doc 03 §5)
         * @description Events: `state`, `progress`, `log`, `usage`, `done`, `reset`; data payloads are the
         *     Sse* schemas. Every event has `id: <epoch>-<seq>`. Reconnect with `Last-Event-ID`: same
         *     epoch and seq still buffered (last 200) resumes; otherwise the server sends `reset`
         *     and the client reloads GET /api/v1/tasks/{id}. usage and progress carry cumulative
         *     values. Progress is throttled to 2/s and logs to 20/s; the full log is in /logs.
         *     Clients must keep working without SSE by polling every 5 s.
         */
        get: operations["taskEvents"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/healthz": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Liveness; no authentication, no dependency checks */
        get: operations["healthz"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/readyz": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Readiness; no authentication (doc 09 §2.2) */
        get: operations["readyz"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
}
export type webhooks = Record<string, never>;
export interface components {
    schemas: {
        Error: {
            error: {
                /** @enum {unknown} */
                code: "validation_failed" | "unauthorized" | "not_found" | "task_state_conflict" | "subtask_active" | "credential_in_use" | "backend_in_use" | "dataset_in_use" | "name_taken" | "preflight_expired" | "precheck_failed" | "source_changed" | "result_changed" | "confirm_path_mismatch" | "model_check_failed" | "idempotency_conflict" | "precondition_failed" | "method_not_allowed" | "internal";
                /** @description Chinese, shown to people as is */
                message: string;
                details?: Record<string, unknown>;
            };
        };
        Link: {
            /** @enum {unknown} */
            rel: "task" | "report" | "adjudication" | "plan" | "logs" | "credentials" | "dataset";
            title: string;
            url: string;
            /**
             * @description false when publicBaseUrl is not configured
             * @default true
             */
            absolute?: boolean;
        };
        Links: components["schemas"]["Link"][];
        CursorPage: {
            items: unknown[];
            next_cursor: string | null;
            has_more: boolean;
        };
        Region: string;
        ModuleId: string;
        /**
         * @description Ark's native values; null = the field is not sent (the model's default, and the parity setting)
         * @enum {unknown}
         */
        ReasoningEffort: "none" | "minimal" | "low" | "medium" | "high" | "xhigh" | "max" | null;
        /** @enum {unknown} */
        VerifyState: "unverified" | "ok" | "failed";
        VerifyResult: {
            verify_state: components["schemas"]["VerifyState"];
            last_verified_at: number;
            error?: string | null;
        };
        /** @description error.details of precheck_failed - one entry per pre-start check (D30) */
        PrecheckDetails: {
            checks: {
                /** @enum {unknown} */
                id: "input" | "output" | "vlm";
                ok: boolean;
                /** @description stable reason of a failed check, e.g. forbidden, not_found, unreachable */
                code: string;
                /** @description Chinese, shown under the form field */
                reason: string;
                /** @description what was checked: a tos:// path or the model */
                target?: string;
                elapsed_ms?: number;
            }[];
        };
        ProbeResult: {
            ok: boolean;
            /** @description Why it failed; with ok true only leftover appears (written, but the probe object could not be removed), which deserves a warning. */
            error?: {
                /** @enum {unknown} */
                code?: "forbidden" | "not_found" | "auth_failed" | "unreachable" | "server_error" | "failed" | "leftover";
                message?: string;
            };
        };
        Credential: {
            id: string;
            name: string;
            /** @constant */
            kind: "tos";
            meta: {
                region: components["schemas"]["Region"];
                endpoint?: string;
                /** @description HeadBucket target when the key cannot ListBuckets */
                test_bucket?: string;
                /** @description last 4 characters only */
                access_key_id_hint?: string;
            };
            verify_state: components["schemas"]["VerifyState"];
            last_verified_at?: number | null;
            last_verify_error?: string | null;
            references?: {
                active_tasks?: number;
                historical_tasks?: number;
            };
            /** @description the key a new task starts with; at most one per owner. Without one the form picks as before (the only key, or the one used last). Deleting the key leaves none. */
            is_default: boolean;
            created_at: number;
            updated_at: number;
        };
        CredentialCreate: {
            name: string;
            access_key_id: string;
            secret_access_key: string;
            region: components["schemas"]["Region"];
            endpoint?: string;
            test_bucket?: string;
            /** @description temporary credentials only; empty = none */
            session_token?: string;
        };
        CredentialUpdate: {
            name?: string;
            access_key_id?: string;
            /** @description empty = unchanged */
            secret_access_key?: string;
            region?: components["schemas"]["Region"];
            endpoint?: string;
            test_bucket?: string;
            /** @description temporary credentials only; empty = none */
            session_token?: string;
            /** @description true makes this key the default and takes the flag from any other key; false clears it when this key has it. Neither verifies the key again. */
            is_default?: boolean;
        };
        VlmModel: {
            id: string;
            /** @description the model a new task starts with; at most one per owner across every backend. Without one the form asks, as before. Deleting the model or its backend leaves none. */
            is_default: boolean;
            /** @description Model ID or inference endpoint ID (ep-...) */
            model_name: string;
            reasoning_effort: components["schemas"]["ReasoningEffort"];
            /** @description null = the backend's */
            max_concurrency: number | null;
            capabilities: {
                vision?: boolean | null;
                /** @description effective levels to offer (from the site's mapping table; all 7 when unknown) */
                reasoning_effort_levels?: ("none" | "minimal" | "low" | "medium" | "high" | "xhigh" | "max")[];
            };
            /** @enum {unknown} */
            source: "listed" | "manual";
        };
        VlmModelCreate: {
            model_name: string;
            reasoning_effort?: components["schemas"]["ReasoningEffort"];
            max_concurrency?: number | null;
        };
        VlmModelPatch: {
            reasoning_effort?: components["schemas"]["ReasoningEffort"];
            max_concurrency?: number | null;
            /** @description true makes it the one default and clears the flag on every other model of every backend; false clears it and leaves none */
            is_default?: boolean;
        };
        VlmBackend: {
            id: string;
            name: string;
            /** @enum {unknown} */
            kind: "ark" | "custom";
            endpoint: string;
            /** @default 64 */
            max_concurrency: number;
            has_api_key: boolean;
            verify_state: components["schemas"]["VerifyState"];
            last_verified_at?: number | null;
            last_verify_error?: string | null;
            models: components["schemas"]["VlmModel"][];
            /** @description whether GET /models worked last time */
            models_listed?: boolean;
            created_at: number;
            updated_at: number;
        };
        VlmBackendCreate: {
            name: string;
            /** @enum {unknown} */
            kind: "ark" | "custom";
            endpoint: string;
            /** @description required for ark */
            api_key?: string;
            max_concurrency?: number;
        };
        VlmBackendUpdate: {
            name?: string;
            endpoint?: string;
            /** @description empty = unchanged */
            api_key?: string;
            max_concurrency?: number;
        };
        /** @description C1 2.0 (design doc 17 §2): where every module runs, what it can find and what it covers. The console takes every module, code and item name from here. */
        ModuleRegistry: {
            registry_version: string;
            /** @description the taxonomy (C6) every code maps into */
            taxonomy_version: string;
            /** @description The two blocks that run side by side and never filter each other (D57); inside a block every stage gets every selected episode */
            blocks: {
                /** @enum {unknown} */
                id: "cpu" | "vlm";
                title_zh: string;
                stages: components["schemas"]["RegistryStage"][];
            }[];
            /** @description every stage, the CPU block's first */
            stages: components["schemas"]["RegistryStage"][];
            /** @description stages that need the whole selection and start once the earlier stages of their block are done */
            full_set_stages: components["schemas"]["RegistryStage"][];
            /** @description blocking (rejects), review (asks a person), info (reported only), with the console's titles */
            finding_levels: {
                id: components["schemas"]["FindingLevel"];
                title_zh: string;
            }[];
            /** @description why a module could not assess an item on an episode (design doc 17 §1.4) */
            unassessable_reasons: {
                id: string;
                title_zh: string;
            }[];
            /** @description The questions a person can be asked on the adjudication page (D43). A page shows a line it has no dedicated view for from this entry: its title, the question's reason and one button per decision. */
            review_lines: components["schemas"]["ReviewLine"][];
            /** @description the bound taxonomy (C6, docs/contracts/taxonomy.json) without its sample-set notes */
            taxonomy: {
                dimensions: {
                    id: string;
                    name_zh: string;
                }[];
                items: {
                    id: components["schemas"]["TaxonomyItemId"];
                    dimension: string;
                    name_zh: string;
                    explain_zh: string;
                    /**
                     * @description control: a legal case that must not be reported
                     * @enum {unknown}
                     */
                    kind: "defect" | "phenomenon" | "reference" | "control";
                    /** @enum {unknown} */
                    level: "episode" | "dataset";
                }[];
            };
            modules: {
                id: components["schemas"]["ModuleId"];
                name_zh: string;
                summary_zh: string;
                /** @enum {unknown} */
                level: "episode" | "dataset";
                needs: ("timestamps" | "action" | "state" | "video" | "embodiment_profile" | "vlm" | "raw_bytes" | "eef_input")[];
                /** @enum {unknown} */
                block: "cpu" | "vlm";
                stage: components["schemas"]["RegistryStage"];
                /** @description data only: the captions autolabel writes for episodes without a task text */
                depends_on: "autolabel"[];
                /** @description the finding codes the module reports (design doc 17 §2.2) */
                codes: components["schemas"]["FindingCode"][];
                /** @description the items it assesses on an episode it can read: its codes' items plus items it only reports a reading for; a record says which of them it could not assess and why */
                covers: components["schemas"]["TaxonomyItemId"][];
                param_schema: Record<string, unknown>;
                tables: {
                    id: string;
                    title_zh: string;
                    sortable: string[];
                    default_sort: string;
                }[];
                mergeable: boolean;
                /** @description present on a module answered inside this host module's requests (1.19.0): it runs whenever the host runs, has no request of its own and cannot be selected alone */
                rides_on?: components["schemas"]["ModuleId"];
            }[];
        };
        /**
         * @description a stage of a block (registry 2.0); a plan's stage ids are its own (cli/plan.schema.json)
         * @enum {unknown}
         */
        RegistryStage: "integrity" | "numeric" | "frame" | "dedup" | "autolabel" | "vlm" | "profile";
        /** @enum {unknown} */
        FindingLevel: "blocking" | "review" | "info";
        /** @description an item of the taxonomy (C6), e.g. FILE-3, MV-5 */
        TaxonomyItemId: string;
        FindingCode: {
            /** @description unique within its module */
            code: string;
            /** @description null only for an info-level item of the platform's own */
            item: components["schemas"]["TaxonomyItemId"] | null;
            name_zh: string;
            /**
             * @description the default; a module may grade by measure
             * @enum {unknown}
             */
            severity: "high" | "medium" | "low";
            level: components["schemas"]["FindingLevel"];
            /** @enum {unknown} */
            scope_kind: "episode" | "camera" | "channel" | "dataset";
            /** @description blocking codes only: a reject it caused may be appealed (D42) */
            appealable: boolean;
            review_line?: components["schemas"]["ReviewLineId"];
        };
        /** @description a line of the registry's review_lines; today label, task_verdict, reject_appeal, eef_check, integrity_check */
        ReviewLineId: string;
        ReviewLine: {
            id: components["schemas"]["ReviewLineId"];
            /** @description the kind of its review.json items (C2); label_conflict for label */
            review_kind: string;
            title_zh: string;
            /**
             * @description the list its episodes are in when asked
             * @enum {unknown}
             */
            applies_to: "passed" | "reject";
            /** @description an open item must be decided; false = a person may act (appeals) */
            counts_as_pending: boolean;
            decisions: {
                const: string;
                title: string;
            }[];
            /**
             * @description Questions a card gains once its answer on this line is one of `after` - only on a card that does not ask that line already. v1: after adopting a new label a person may give the task verdict (the machine takes it, no re-judging); left open, the episode is judged again with the new label. An optional follow-up never counts as pending, and its answer lapses (not executed, not counted) once the answer that opened it changes.
             * @default []
             */
            follow_ups?: {
                after: string[];
                line: components["schemas"]["ReviewLineId"];
                decisions: string[];
                optional: boolean;
            }[];
        };
        BrowsedDataset: {
            name: string;
            uri: string;
            /** @enum {unknown} */
            format_hint?: "lerobot_v2" | "lerobot_v3" | "mcap" | "lance" | "rrd" | "unknown";
            episodes?: number | null;
        };
        /** @enum {unknown} */
        DatasetFormat: "lerobot_v2" | "lerobot_v3" | "mcap" | "lance" | "unsupported";
        DatasetItemFields: {
            /** @description ds- and 9 lowercase letters (D45); registrations made before 1.10.0 keep ds_ and their old id */
            id: string;
            name: string;
            /** @enum {unknown} */
            source: "tos" | "public" | "local";
            uri: string;
            region: string | null;
            format: components["schemas"]["DatasetFormat"];
            episode_count: number | null;
            robot_type: string | null;
            /**
             * @description changed: the fingerprints differ from the kept ones; preflight again
             * @enum {unknown}
             */
            check_state: "ok" | "changed";
            checked_at: number | null;
            preflighted_at: number;
            created_at: number;
            last_task: null | components["schemas"]["TaskRef"];
            viz: components["schemas"]["VizStatus"];
            /** @description the mcap field mapping (C7) - its state, version and name, shown under the format in the list; null for other formats */
            viz_mapping: null | components["schemas"]["DatasetMappingInfo"];
        };
        DatasetItem: components["schemas"]["DatasetItemFields"];
        DatasetDetail: components["schemas"]["DatasetItemFields"] & {
            note: string | null;
            /** @description the external annotation file attached to it (a viz_annotations upload), if any */
            annotations: null | components["schemas"]["DatasetAnnotationsInfo"];
            /** @description access key name; null for the public bucket, or once the key is deleted */
            credential: string | null;
            preflight: components["schemas"]["preflight.schema"];
            meta_fingerprint: string;
            /** @description summary of the file listing kept at the last preflight (source-manifest summary) */
            listing: {
                /** @description C2 source-manifest summary.count */
                objects: number;
                bytes: number;
                digest: string;
            };
            /** @description newest first */
            checks: components["schemas"]["DatasetCheck"][];
            /** @description newest first */
            tasks: components["schemas"]["TaskRef"][];
            links: components["schemas"]["Links"];
        };
        DatasetCreate: {
            input: components["schemas"]["InputRef"];
            /** @description defaults to the last path segment */
            name?: string;
            note?: string;
            /** @description mcap only - the field mapping confirmed in the add drawer (kept as version 1) */
            viz_mapping?: components["schemas"]["viz-mapping.schema"];
            /** @description an external annotation file (a viz_annotations upload) to attach */
            annotations_upload?: components["schemas"]["UploadId"];
        };
        DatasetPatch: {
            name?: string;
            note?: string | null;
        };
        DatasetCheck: {
            at: number;
            /** @enum {unknown} */
            trigger: "add" | "recheck" | "task_start" | "repreflight";
            /** @enum {unknown} */
            result: "same" | "changed";
            change: null | components["schemas"]["SourceChange"];
        };
        /** @description what differs from the fingerprints kept at registration or at the last preflight (D37) */
        SourceChange: {
            meta_changed: boolean;
            added: number;
            removed: number;
            modified: number;
            /** @description a few affected keys, added ones first */
            sample_keys: string[];
            preflighted_at: number;
        };
        TaskRef: {
            id: string;
            name: string;
            state: components["schemas"]["TaskState"];
            created_at: number;
        };
        RepreflightResult: {
            compatible: boolean;
            incompatibilities: {
                /** @description form field to fix, e.g. modules, episodes, embodiment_id */
                field: string;
                module?: components["schemas"]["ModuleId"];
                reason_code: string;
                /** @description Chinese, shown as is */
                reason: string;
            }[];
            task: components["schemas"]["Task"];
        };
        /** @description Soft-deleted tasks never count, except in tokens. running counts what workers are busy with - main runs and subtasks in running, pausing or stopping - and queued / paused those states; recent covers the period `days` asked for in the site's time zone (CURATOR_TZ_OFFSET), cut into buckets that end with the current one (7 or 30 days, 13 calendar weeks from Monday, or 12 calendar months) and starting at `since`: tasks that ended succeeded or completed_with_errors since then, their summary totals, passed / total over episodes, and actual-ledger prompt + completion tokens per bucket; credentials_failed counts access keys (kind tos) only, a backend's key failing counts in backends_failed. */
        Overview: {
            todo: {
                /** @description completed_with_errors, can be retried */
                error_tasks: number;
                adjudication: {
                    tasks: number;
                    episodes: number;
                };
                /** @description tasks whose delivered dataset is stale or was never exported */
                delivery_pending: number;
                datasets_changed: number;
                credentials_failed: number;
                backends_failed: number;
            };
            running: {
                running: number;
                queued: number;
                paused: number;
                active: {
                    task: components["schemas"]["TaskRef"];
                    stage: string | null;
                    done: number;
                    total: number;
                }[];
            };
            recent: {
                /**
                 * @description the period asked for
                 * @enum {unknown}
                 */
                days: 7 | 30 | 90 | 365;
                /**
                 * @description day for 7 and 30, week for 90, month for 365
                 * @enum {unknown}
                 */
                bucket: "day" | "week" | "month";
                /** @description epoch ms where the first bucket starts; the figures count from here */
                since: number;
                tasks_finished: number;
                episodes_checked: number;
                pass_rate: number | null;
                /** @description actual-ledger totals only, oldest bucket first, every bucket listed (7, 30, 13 or 12) */
                tokens_per_bucket: {
                    /** @description epoch ms, local midnight in the site's time zone */
                    start: number;
                    /** @description 09-23 (a day), 09-21 周 (the week from that Monday), 2026-09 (a month) */
                    label: string;
                    tokens: number;
                }[];
            };
            datasets: {
                total: number;
                changed: number;
            };
            generated_at: number;
        };
        EpisodePreview: {
            index: number;
            length_s: number | null;
            task: string;
            /** @enum {unknown} */
            task_source: "原始标注" | "无";
            /** @description mcap (1.12): the episode has a task topic the preview does not read (its text is inside the file; the checks read it) - task is empty, but not because there is none */
            task_unread?: boolean;
            cameras: {
                name: string;
                /** @description presigned, or anonymous for the public bucket */
                url: string;
                from_ts?: number;
                to_ts?: number;
            }[];
        };
        InputRef: {
            /** @enum {unknown} */
            source: "tos" | "public" | "local";
            uri: string;
            region?: components["schemas"]["Region"];
            /** @description access key name; not for source=public. Requests name one; responses give null once the key was deleted (see rebind-credentials) */
            credential?: string | null;
        };
        /** @description a registered dataset (D36), or an input given in full */
        InputSpec: {
            dataset_id: string;
        } | components["schemas"]["InputRef"];
        OutputRef: {
            uri: string;
            region?: components["schemas"]["Region"];
            /** @description access key name; null in responses once the key was deleted */
            credential: string | null;
        };
        /**
         * @description the x-upload-kind of a file parameter (registry 1.5); viz_annotations is a dataset's external annotation file (design doc 18 §4.5), not a module parameter
         * @enum {unknown}
         */
        UploadKind: "eef_trajectory" | "eef_observation_seeds" | "eef_gripper_template" | "eef_record_mapping" | "viz_annotations";
        /** @description upl- and 9 lowercase letters (D45); uploads made before 1.10.0 keep upl_ and hex digits */
        UploadId: string;
        UploadIssue: {
            /** @description JSON path inside the file */
            field?: string | null;
            problem: string;
            code?: string;
            /** @enum {unknown} */
            severity?: "error" | "warning";
            sample_id?: string;
            episode_index?: number;
            frame_index?: number;
            camera_id?: string;
            point_id?: string;
        };
        Upload: {
            upload_id: components["schemas"]["UploadId"];
            /** @description the value of the file parameter in TaskCreate.modules[].params */
            handle: string;
            kind: components["schemas"]["UploadKind"];
            name: string;
            sha256: string;
            size_bytes: number;
            created_at: number;
            validation: {
                /** @constant */
                valid: true;
                /** @description eef_trajectory: dataset, samples, episodes, frames, cameras, points_checked, max_reprojection_difference_px; eef_observation_seeds: rows, samples, cameras, points */
                summary: Record<string, unknown>;
                warnings: components["schemas"]["UploadIssue"][];
            };
        };
        PreflightRequest: {
            input: components["schemas"]["InputSpec"];
            vlm_backend?: string;
            embodiment_id?: string;
        };
        PreflightResponse: {
            preflight_id: string;
            expires_at: number;
            result: components["schemas"]["preflight.schema"];
        };
        DeliveryProbeRequest: {
            uri: string;
            region?: components["schemas"]["Region"];
            credential: string;
        };
        /** @enum {unknown} */
        TaskState: "created" | "queued" | "running" | "pausing" | "paused" | "stopping" | "stopped" | "succeeded" | "completed_with_errors" | "failed";
        EpisodeSelector: {
            /** @constant */
            mode: "all";
        } | {
            /** @constant */
            mode: "head";
            n: number;
        } | {
            /** @constant */
            mode: "explicit";
            /** @description 3,10-12 / @file is CLI only; validated and expanded on the server */
            expr: string;
            readonly indices?: number[];
        };
        ModuleChoice: components["schemas"]["ModuleId"] | {
            id: components["schemas"]["ModuleId"];
            /** @description validated against the module's param_schema */
            params?: Record<string, unknown>;
        };
        TaskVlm: {
            backend: string;
            model: string;
            reasoning_effort?: components["schemas"]["ReasoningEffort"];
            /** @description effective settings frozen at start (P17) */
            snapshot?: Record<string, unknown> | null;
        };
        VlmChoice: {
            backend: string;
            model: string;
            reasoning_effort?: components["schemas"]["ReasoningEffort"];
        };
        /** @description task-level parameters (doc 03 §3.1); concurrency values are upper bounds only (D31) */
        TaskParams: {
            /** @default true */
            start_now?: boolean;
            /** @default true */
            export?: boolean;
            /** @description Maximum episodes per funnel dispatch. Persistent workers hand off completed episodes immediately and refill up to the plan concurrency, independently of this size. External CLI wrappers retain batch handoff. If omitted, derived from parallelism (8–64) and reduced for small selections. */
            batch_size?: number;
            /** @default 3 */
            vlm_retry?: number;
            /** @default true */
            vlm_hedge?: boolean;
            vlm_timeouts_s?: {
                /** @default 60 */
                probe?: number;
                /** @default 60 */
                endstate?: number;
                /** @default 60 */
                arbitration?: number;
                /** @default 60 */
                caption?: number;
                /** @default 120 */
                llm?: number;
            };
            /** @default false */
            clips?: boolean;
            /** @description The verdict policy (design doc 17 §4, D58): which findings reject an episode, which ask a person and which are only reported. `default` gives every finding code its registry level (today's hard gates reject, today's suspects and abstentions are asked); `report_only` rejects nothing and asks nothing. Frozen at start; changing it later re-runs aggregate only. The funnel keeps judging by `default` until the policy aggregate replaces it (F12.3). */
            policy?: {
                /**
                 * @default default
                 * @enum {unknown}
                 */
                preset?: "default" | "report_only";
            };
            limits?: {
                /** @description At most this many CPU workers for this task; it can only lower the default, which is every core of the container but two (P4, D54). Unset: that default. The CPU workers of all running tasks come out of one pool of that size in the Daemon, so fewer may run at a time while other tasks run too. */
                cpu_concurrency?: number;
                vlm_parallelism?: number;
            };
        };
        TaskCreate: {
            name: string;
            note?: string;
            input: components["schemas"]["InputSpec"];
            output: components["schemas"]["OutputRef"];
            preflight_id: string;
            episodes: components["schemas"]["EpisodeSelector"];
            modules: components["schemas"]["ModuleChoice"][];
            embodiment_id?: string;
            vlm?: components["schemas"]["VlmChoice"];
            params?: components["schemas"]["TaskParams"];
        };
        TaskBatchCreate: {
            items: {
                name: string;
                input: components["schemas"]["InputSpec"];
                output: components["schemas"]["OutputRef"];
                preflight_id: string;
            }[];
            shared: {
                note?: string;
                episodes: components["schemas"]["EpisodeSelector"];
                modules: components["schemas"]["ModuleChoice"][];
                embodiment_id?: string;
                vlm?: components["schemas"]["VlmChoice"];
                params?: components["schemas"]["TaskParams"];
            };
        };
        TaskCreated: {
            id: string;
            state: components["schemas"]["TaskState"];
            created_at: number;
            warnings: string[];
            links: components["schemas"]["Links"];
        };
        /** @description after start only name and note are accepted */
        TaskPatch: {
            name?: string;
            note?: string;
            input?: components["schemas"]["InputSpec"];
            output?: components["schemas"]["OutputRef"];
            preflight_id?: string;
            episodes?: components["schemas"]["EpisodeSelector"];
            modules?: components["schemas"]["ModuleChoice"][];
            embodiment_id?: string | null;
            vlm?: components["schemas"]["VlmChoice"];
            params?: components["schemas"]["TaskParams"];
        };
        StageProgress: {
            /** @description two-block plans: integrity, numeric, frame, dedup, autolabel, vlm, profile, final; funnel plans (tasks made before): also verdict and profile_vlm; then report, export, verify; subtasks add adjudicate; new modules may add stages */
            id: string;
            /**
             * @description the block the stage belongs to (two-block plans, design doc 17 §3); absent on funnel plans and on the steps after both blocks
             * @enum {unknown}
             */
            block?: "cpu" | "vlm";
            /** @description a full-set stage: it starts once the earlier stages of its block are done (dedup, profile) */
            full_set?: boolean;
            /** @enum {unknown} */
            state: "pending" | "running" | "succeeded" | "completed_with_errors" | "failed" | "skipped";
            done: number;
            total: number;
            elapsed_s?: number | null;
            eta_s?: number | null;
            /** @description e.g. why total dropped from 50 to 49 */
            note?: string;
            pipeline?: components["schemas"]["PipelineActivity"];
        };
        /** @description Current invocation of the persistent funnel. Inflight counts admitted episodes awaiting durable completion, including worker setup. Dispatches are layer-local arrivals, not a barrier shared across layers. */
        PipelineActivity: {
            inflight: number;
            queued: number;
            capacity: number;
            dispatches: number;
            /** @description Epoch milliseconds */
            started_at: number | null;
            /** @description Epoch milliseconds */
            finished_at: number | null;
            /** @description Epoch milliseconds of this snapshot */
            updated_at: number;
            /** @description Per-episode processing durations; excludes dispatch and CPU admission waits. Shared module execution is counted once per episode in this layer. */
            processing?: {
                count: number;
                total_s: number;
                mean_s: number | null;
                min_s: number | null;
                max_s: number | null;
            };
            /** @description Spans (epoch milliseconds) during which this layer had episodes out, oldest first; the last one is open (end null) while episodes are out now. Between started_at and finished_at (or now) the rest is waiting. Past 64 spans the two separated by the shortest wait are merged. */
            busy?: {
                start: number;
                end: number | null;
            }[];
            /** @description The layer has episodes waiting to enter but hands none out because the next layer's queue is full (it holds at most max(2 x batch size, that layer's width)). */
            held_by_downstream?: boolean;
            recent: {
                number: number;
                count: number;
                /** @description First 16 episode indices of this dispatch */
                episodes: number[];
                /** @description Epoch milliseconds of dispatch */
                at: number;
            }[];
        };
        ModuleState: {
            id: components["schemas"]["ModuleId"];
            name: string;
            selected: boolean;
            /** @enum {unknown} */
            availability: "available" | "needs_input" | "unsupported";
            unavailable_reason?: string | null;
            /** @enum {unknown} */
            state: "pending" | "running" | "succeeded" | "completed_with_errors" | "failed" | "skipped" | "stale";
            episodes_total: number;
            episodes_error: number;
            elapsed_s?: number | null;
            error?: string | null;
        };
        Summary: {
            total: number;
            passed: number;
            rejected: number;
            held: number;
            review: number;
            pass_rate: number | null;
            /** @description episodes left out because source files are missing (D40, as v1 does): not checked, in none of the lists and not part of total; the report lists them */
            skipped?: number;
        };
        UsageTotals: {
            prompt_tokens: number;
            completion_tokens: number;
            reasoning_tokens: number;
            cached_tokens: number;
            requests: number;
            requests_unknown_usage: number;
        };
        TaskListItem: {
            id: string;
            name: string;
            state: components["schemas"]["TaskState"];
            /** @enum {unknown} */
            pause_reason?: "user" | "system" | null;
            dataset: string;
            dataset_id?: string | null;
            created_at: number;
            progress: {
                stages: components["schemas"]["StageProgress"][];
            };
            summary: null | components["schemas"]["Summary"];
            pending_adjudication: number;
            delivery_stale: boolean;
            active_subtask?: string | null;
            /** @description selected modules, registry order */
            modules: components["schemas"]["ModuleId"][];
            /** @description selected modules per ModuleState state */
            module_counts: {
                [key: string]: number;
            };
            usage: components["schemas"]["UsageTotals"];
            deleted_at?: number | null;
        };
        Task: {
            id: string;
            name: string;
            note?: string | null;
            state: components["schemas"]["TaskState"];
            state_reason?: string | null;
            /** @enum {unknown} */
            pause_reason?: "user" | "system" | null;
            input: components["schemas"]["InputRef"];
            /** @description the registered dataset (D36) */
            dataset_id?: string | null;
            output: components["schemas"]["OutputRef"];
            run_id?: string | null;
            episodes: components["schemas"]["EpisodeSelector"];
            embodiment_id?: string | null;
            vlm?: null | components["schemas"]["TaskVlm"];
            params: components["schemas"]["TaskParams"];
            /** @description source manifest summary frozen at start (D27) */
            source?: {
                objects?: number;
                bytes?: number;
                digest?: string;
            } | null;
            progress: {
                stages: components["schemas"]["StageProgress"][];
            };
            modules: components["schemas"]["ModuleState"][];
            summary?: null | components["schemas"]["Summary"];
            result_rev: number;
            usage: components["schemas"]["UsageTotals"];
            pending_adjudication: number;
            delivery_stale: boolean;
            active_subtask?: null | components["schemas"]["Subtask"];
            created_at: number;
            updated_at: number;
            started_at?: number | null;
            finished_at?: number | null;
            deleted_at?: number | null;
            links: components["schemas"]["Links"];
        };
        Subtask: {
            id: string;
            task_id: string;
            /** @enum {unknown} */
            kind: "retry" | "resume" | "apply_adjudication" | "reexport";
            scope: {
                modules?: components["schemas"]["ModuleId"][];
                /** @enum {unknown} */
                episodes?: "errors" | "all";
                /**
                 * @description apply_adjudication only (D39)
                 * @enum {unknown}
                 */
                relabel_rerun?: "v1" | "full";
            };
            state: components["schemas"]["TaskState"];
            state_reason?: string | null;
            /**
             * @description only while pausing or paused
             * @enum {unknown}
             */
            pause_reason?: "user" | "system" | null;
            progress?: Record<string, unknown> | null;
            created_at: number;
            started_at?: number | null;
            finished_at?: number | null;
            /** @description the revision this subtask produced */
            result_rev?: number | null;
        };
        SubtaskCreated: {
            subtask: components["schemas"]["Subtask"];
            links: components["schemas"]["Links"];
        };
        TimelineEntry: {
            at: number;
            /** @enum {unknown} */
            kind: "created" | "started" | "system_pause" | "system_resume" | "user_pause" | "user_resume" | "stopped" | "failed" | "finished" | "subtask_started" | "subtask_finished" | "revision";
            subtask_id?: string | null;
            revision?: number | null;
            state?: null | components["schemas"]["TaskState"];
            text: string;
        };
        LogLine: {
            ts: number;
            stage: string;
            subtask_id?: string | null;
            /** @enum {unknown} */
            level: "error" | "warn" | "info" | "debug";
            msg: string;
            episode_index?: number | null;
        };
        UsageRow: {
            /** @description empty = main run */
            subtask_id: string;
            module_id: string;
            /** @description v1: probe, endstate, arbitration, caption, llm; merged; new modules name their own */
            call_kind: string;
            model_name: string;
            prompt_tokens: number;
            completion_tokens: number;
            reasoning_tokens: number;
            cached_tokens: number;
            requests: number;
            requests_unknown_usage: number;
        };
        /** @description totals sum the actual ledger only; attributed splits merged requests across modules and must never be added to it */
        UsageReport: {
            totals: components["schemas"]["UsageTotals"];
            actual: components["schemas"]["UsageRow"][];
            attributed: components["schemas"]["UsageRow"][];
        };
        PipelineEpisode: {
            episode_index: number;
            /**
             * @description a funnel run (tasks made before 2.2.0): the last stage it went through
             * @enum {unknown}
             */
            last_stage?: "integrity" | "numeric" | "frame" | "vlm";
            /**
             * @description a funnel run: where it goes next
             * @enum {unknown}
             */
            next_stage?: "numeric" | "frame" | "vlm" | "done";
            /** @description missing: left out, its source files are gone (D40); a funnel run's gate: stopped there */
            reason: string | null;
            /** @enum {string|null} */
            verdict: "keep" | "drop" | "held" | null;
            verdict_reason: string | null;
            /** @description two-block runs (design doc 17 §5.5): every per-episode stage of both blocks - done (or error, when one of its modules failed on the episode), running, or waiting */
            stages?: {
                [key: string]: "waiting" | "running" | "done" | "error";
            };
            /** @description the verdict is the policy applied to the findings so far, not the final one (two-block pipelines: the final verdict is only given by aggregate) */
            provisional?: boolean;
            /** @description Sum of completed layer processing times for this episode; excludes queue and CPU admission waits */
            processing_s?: number | null;
            stage_processing_s?: {
                integrity?: number;
                numeric?: number;
                frame?: number;
                vlm?: number;
            };
            /** @description Present on the single-episode endpoint */
            modules?: {
                [key: string]: components["schemas"]["result-record.schema"];
            };
        };
        EpisodeView: {
            episode_index: number;
            revision: number;
            /** @enum {unknown} */
            list: "passed" | "reject" | "held";
            /** @description why it is in its list */
            reasons?: components["schemas"]["EpisodeNote"][];
            /** @description what a person is asked about it, questions of the adjudication page first; the abstentions of other modules follow (shown, never queued) */
            review?: components["schemas"]["EpisodeNote"][];
            task_text?: {
                text?: string;
                source?: string;
            };
            /** @description module id -> result record (cli/result-record.schema.json) */
            modules: {
                [key: string]: components["schemas"]["result-record.schema"];
            };
            evidence?: {
                module: string;
                /** @description sign with scope=delivery */
                path: string;
                kind?: string;
            }[];
            videos: {
                camera: string;
                /** @enum {unknown} */
                scope: "delivery" | "input";
                /** @enum {unknown} */
                origin?: "clip" | "delivery_dataset" | "source_dataset";
                path: string;
                from_ts?: number;
                to_ts?: number;
            }[];
            /** @description findings revisions (C2 2.0, 2.3.0): every finding of the episode, every module, with the level it has in this revision (its list entry's: the task's policy after the applied human decisions) */
            findings?: components["schemas"]["EpisodeFinding"][];
            /** @description 2.4.0: the dataset's fps from the task's preflight, to read a finding's frames as seconds; null for mcap (its checks run at each episode's action rate: getTaskEpisodeViz check_clock) */
            fps?: number | null;
            /** @description 2.4.0, the task's registration while it exists (the mini player's 在可视化页打开) */
            dataset_id?: string | null;
        };
        EpisodeFinding: {
            module: components["schemas"]["ModuleId"];
            level: components["schemas"]["FindingLevel"];
            /** @description review: the line it is asked on */
            line?: components["schemas"]["ReviewLineId"];
            /** @description a reject it causes may be appealed (D42) */
            appealable: boolean;
            /** @description a person's conclusion (adjudication), not a module's finding */
            human?: boolean;
            finding: components["schemas"]["finding"];
        };
        TaskEpisode: {
            episode_index: number;
            /** @enum {unknown} */
            list: "passed" | "reject" | "held";
            /** @description a person still has to answer something about it (see listTaskEpisodes) */
            review: boolean;
            /** @description the modules that put it in its list - the deciding modules of a reject, the failed modules of a held one, none when passed */
            reason_modules: components["schemas"]["ModuleId"][];
            /** @description the source modules of the open questions */
            review_modules: components["schemas"]["ModuleId"][];
            /** @description findings revisions (C2 2.0) only - the taxonomy items of its findings, every module together, in taxonomy order */
            items?: components["schemas"]["TaxonomyItemId"][];
            /** @description findings revisions (C2 2.0) only - the levels its findings have (the task's policy after the applied human decisions), blocking first */
            levels?: components["schemas"]["FindingLevel"][];
        };
        TaskEpisodePage: {
            items: components["schemas"]["TaskEpisode"][];
            next_cursor: string | null;
            has_more: boolean;
            /** @description episodes matching the filters */
            total: number;
            /** @description the whole revision, filters ignored */
            counts: {
                all: number;
                passed: number;
                reject: number;
                held: number;
                review: number;
            };
            revision: number;
        };
        SyncCurves: {
            episode_index: number;
            revision: number;
            /** @description the episode-level reading: aligned, annotated, undecidable, misaligned_all */
            verdict: string | null;
            /** @description the lag every trusted camera agrees on, when they do */
            consensus_lag_s?: number | null;
            /** @description readings within +/- this are aligned (the green band) */
            lag_tol_s: number;
            /** @description the cross-correlation is drawn for lags within +/- this */
            window_s: number;
            cameras: components["schemas"]["SyncCurveCamera"][];
        };
        SyncCurveCamera: {
            camera: string;
            /** @description seconds from the episode start */
            t: (number | null)[];
            /** @description picture motion (optical-flow energy) at t */
            flow: (number | null)[];
            /** @description arm motion (joint speed) at t */
            speed: (number | null)[];
            /** @description seconds; > 0 = the picture is later than the motion */
            lags: (number | null)[];
            /** @description cross-correlation at each lag */
            xcorr: (number | null)[];
            /** @description the check's reading */
            lag_s?: number | null;
            corr_peak?: number | null;
            /** @description aligned, misaligned, ambiguous_peak, flat_peak, low_corr, no_motion */
            code?: string | null;
            trusted?: boolean | null;
            /** @description the reading on the drawn curve; null when there is none or it lies outside the window */
            peak: null | {
                lag_s: number;
                corr: number;
            };
        };
        /** @description one line of an episode's reasons or review, shaped like C2 final-list reasons: kind is a reason kind of C2 final-list (2.0: finding, human, execution_error, duplicate; 1.0: hard_gate, soft_score, duplicate, human, execution_error) or a review kind; a 2.0 reason names its finding code, the code's taxonomy item and whether it may be appealed */
        EpisodeNote: {
            module: components["schemas"]["ModuleId"];
            kind?: string;
            text: string;
            priority?: string;
            duplicate_of?: number;
            /** @description 2.0: the finding code (registry 2.x) */
            code?: string;
            /** @description 2.0: the taxonomy item of the code (C6), null for a code without one */
            item?: string | null;
            /** @description 2.0: the code may be appealed (D42) */
            appealable?: boolean;
        };
        Perf: {
            revision: number;
            /** @enum {unknown} */
            scope: "all" | "main" | "subtask";
            subtask_id?: string | null;
            /** @description model service used (v1 perf_backend) */
            backend?: Record<string, unknown>;
            /** @description CPU / memory quota (v1 perf_env) */
            container?: Record<string, unknown>;
            latency: {
                /** @description v1: probe, endstate, arbitration, caption, llm; merged; new modules name their own */
                call_kind: string;
                count: number;
                failed?: number;
                hedged?: number;
                p50_s: number;
                p90_s: number;
                p99_s: number;
                /** @description first sent to last returned - never count x mean */
                wall_s: number;
            }[];
            effective_concurrency?: number | null;
            stages: {
                id: string;
                wall_s: number;
                share?: number;
            }[];
            retries?: {
                outer_attempts?: number;
                rescued?: number;
            };
            merge?: {
                requests?: number;
                estimated_unmerged?: number;
            };
            /** @description D26 */
            redone_after_interruption?: number;
        };
        AdjudicationQuestion: {
            line: components["schemas"]["ReviewLineId"];
            source_module: components["schemas"]["ModuleId"];
            reason: string;
            /** @description an appeal of a dedup reject: the episode it duplicates */
            duplicate_of?: number | null;
            /** @description set on a question the card gained as a follow-up of its answer on that line (registry follow_ups), absent or null on the card's own questions; a follow-up whose opening answer changed is left out of the card */
            follow_up_of?: null | components["schemas"]["ReviewLineId"];
            annotation?: string | null;
            caption?: string | null;
            /** @description suggested new label */
            suggestion?: string | null;
            priority?: string | null;
            latest_decision?: null | components["schemas"]["Decision"];
            /** @description findings revisions (2.3.0): the finding codes of the source module the question is about */
            codes?: string[];
            /** @description findings revisions (2.3.0): their taxonomy items */
            items?: components["schemas"]["TaxonomyItemId"][];
        };
        AdjudicationCard: {
            episode_index: number;
            /** @enum {unknown} */
            status: "pending" | "decided" | "unsure" | "applied";
            questions: components["schemas"]["AdjudicationQuestion"][];
        };
        /** @description All three count cards (episodes) over the whole task, not the filtered page. pending and decided cover the cards whose questions belong to lines with counts_as_pending - appeal candidates are optional and never pending; unapplied covers both tabs: cards with an answer that was not executed yet ('unsure' is not one). pending is what the task's pending_adjudication shows. */
        AdjudicationCounts: {
            decided: number;
            pending: number;
            unapplied: number;
        };
        DecisionFields: {
            episode_index: number;
            line: components["schemas"]["ReviewLineId"];
            /** @description one of the line's decisions in the registry, on a question the episode's card has or a follow-up the card's answer on another line opened (registry follow_ups); anything else is 400 validation_failed. Today: label - adopt_suggestion, custom_label, keep_label, unsure, discard; task_verdict - success, failure, unsure, discard; reject_appeal - restore, keep_rejected, unsure; eef_check - consistent, inconsistent, unsure; integrity_check - intact, broken, unsure */
            decision: string;
            /** @description custom_label: required; adopt_suggestion: may be left out, the question's suggestion is taken */
            new_label?: string | null;
            note?: string | null;
        };
        AdjudicationApply: {
            /**
             * @description How task success is judged again for relabelled episodes that have no human task verdict (D39). v1: what v1's rejudge runs - multi-view scoring and the per-camera end-state vote only, no task-type step, camera hints, reject guard or evidence arbitration; verdicts and calls match v1. full: the first run's complete flow, so the same decisions may end differently from v1. Episodes a person judged success or failure are not judged again either way.
             * @default v1
             * @enum {unknown}
             */
            relabel_rerun?: "v1" | "full";
        };
        DecisionInput: components["schemas"]["DecisionFields"];
        Decision: components["schemas"]["DecisionFields"] & {
            id: number;
            decided_by: string;
            decided_at: number;
            applied: boolean;
        };
        SignedUrl: {
            url: string;
            expires_at: number;
            /** @description v3 sources: play url#t=from,to */
            from_ts?: number;
            to_ts?: number;
        };
        DatasetSignRequest: {
            /**
             * @description seconds
             * @default 1800
             */
            ttl?: number;
            requests: components["schemas"]["DatasetSignItem"][];
        };
        /** @description One read to sign: `get` - an object by its full key in the bucket (no leading `/`); `list` - one ListObjectsV2 page under `prefix`, `continuation_token` being the previous page's `NextContinuationToken`. */
        DatasetSignItem: {
            /** @constant */
            op: "get";
            key: string;
        } | {
            /** @constant */
            op: "list";
            prefix: string;
            /** @constant */
            delimiter?: "/";
            continuation_token?: string;
            /** @default 1000 */
            max_keys?: number;
        };
        DatasetSignResponse: {
            /** @description epoch milliseconds, as in SignedUrl */
            expires_at: number;
            /** @description one presigned URL per request, in the same order */
            urls: string[];
        };
        Readiness: {
            /** @enum {unknown} */
            status: "ok" | "not_ready";
            checks: {
                db_writable: boolean;
                master_key: boolean;
                workdir_writable: boolean;
                scratch_writable: boolean;
                reconciled: boolean;
            };
        };
        SseState: {
            state: components["schemas"]["TaskState"];
            /** @enum {unknown} */
            pause_reason?: "user" | "system" | null;
            /** @description set when the change is a subtask's */
            subtask_id?: string | null;
            /** @description state_reason, Chinese */
            reason?: string | null;
            at: number;
        };
        SseProgress: components["schemas"]["StageProgress"];
        SseLog: {
            stage: string;
            /** @enum {unknown} */
            level: "error" | "warn" | "info" | "debug";
            msg: string;
        };
        SseUsage: components["schemas"]["UsageTotals"];
        SseDone: {
            state: components["schemas"]["TaskState"];
            failed_modules?: components["schemas"]["ModuleId"][];
            subtask_id?: string | null;
            reason?: string | null;
        };
        /** @description whether the visualizer can open the dataset */
        VizStatus: {
            /**
             * @description mapping_pending: an mcap dataset whose field mapping is not confirmed (映射待确认)
             * @enum {unknown}
             */
            state: "ready" | "mapping_pending" | "unsupported";
            /** @description why not, in Chinese */
            reason: string | null;
        };
        VizFormat: {
            /**
             * @description as the visualizer reads it: a lerobot-lancedb 0.1-0.2 dataset (which the preflight takes for LeRobot) is lance
             * @enum {unknown}
             */
            kind: "lerobot" | "mcap" | "lance" | "lancedb" | "rrd" | "unknown";
            /** @enum {unknown} */
            version: "v2" | "v3" | null;
            /**
             * @description the reader serving it; null = none (Lance tables without LeRobot metadata, rrd ...)
             * @enum {unknown}
             */
            reader: "lerobot" | "mcap" | "lance" | null;
            /**
             * @description Lance: which lerobot-lancedb layout (design doc 19 §4.2) - 0.3's frames / videos / meta
             *     tables, 0.1-0.2's frames table with a videos table beside it, or with a JPEG column per
             *     camera; null for the other formats. 2.5.0
             * @enum {unknown}
             */
            layout: "lance-0.3" | "lance-0.2-video" | "lance-0.2-frames" | null;
        };
        /**
         * @description How a camera reaches the browser (design doc 18 §4.2, D60). direct: a presigned (or the
         *     public bucket's) TOS URL; local: the Daemon serves the file of a local dataset; remux: the
         *     Daemon rewraps the stream into fragmented mp4 without re-encoding; frames: a JPEG frame pack
         *     drawn on a canvas; transcode: the Daemon re-encodes to H.264 (CURATOR_VIZ_TRANSCODE, the
         *     player tags it 平台转码); blob: the Daemon serves, by Range, the mp4 kept in a Lance table's
         *     blob column (2.5.0, design doc 19 §4.3); unsupported: cannot be shown, `reason` says why.
         * @enum {unknown}
         */
        VizAccess: "direct" | "local" | "remux" | "frames" | "transcode" | "blob" | "unsupported";
        /** @description URL-safe camera key (LeRobot short name; mcap topic with / turned into _) */
        VizCameraKey: string;
        VizCamera: {
            key: components["schemas"]["VizCameraKey"];
            name: string;
            /** @description the LeRobot feature key or the mcap topic */
            source: string;
            /**
             * @description video: an mp4 for <video>; frames: a JPEG frame pack
             * @enum {unknown}
             */
            kind: "video" | "frames";
            access: components["schemas"]["VizAccess"];
            /** @description av1, h264, hevc, mpeg4, jpeg, png, raw ... */
            codec: string | null;
            /** @description RFC 6381 codecs parameter for the player's decoding check (av01..., avc1..., hvc1...) */
            codec_string: string | null;
            width: number | null;
            height: number | null;
            fps: number | null;
            pix_fmt?: string | null;
            /** @description the Daemon re-encodes it (access transcode) */
            transcoded: boolean;
            /** @description why transcode or unsupported, in Chinese */
            reason: string | null;
        };
        VizLine: {
            name: string;
            /**
             * @description state solid, action dashed
             * @enum {unknown}
             */
            role: "state" | "action" | "other";
            /** @description the feature key or topic */
            source?: string;
            /** @description its position in the source's vector */
            dim?: number | null;
            unit?: string | null;
        };
        VizStream: {
            key: string;
            /** @enum {unknown} */
            kind: "series" | "depth" | "pointcloud" | "transform" | "text" | "event" | "image_sequence";
            name: string;
            unit: string | null;
            /** @description series: one per drawn number, state and action of the same name together */
            lines: components["schemas"]["VizLine"][];
            /** @description placed by the smart layout */
            smart: boolean;
            /** @description false: listed (field tree, + menu) but not drawable in this phase */
            available: boolean;
            reason: string | null;
            /** @description feature keys or topics it is read from */
            sources: string[];
            rate_hz?: number | null;
        };
        VizAnnotationSource: {
            key: string;
            /** @enum {unknown} */
            kind: "segments" | "events" | "labels";
            /** @description the track's name (子任务, 机器人分步, 外部标注 ...) */
            name: string;
            /** @description the recognised layout (design doc 18 §4.5): subtask_index, language_persistent, language_events, task_index, index_table, string_column, bool_segments, quality, episode_labels, argus, mcap_topic, mcap_attachment; open for later readers */
            format: string;
            /** @description where it is read from, shown as 来源 */
            source: string;
            /** @description false: something looks like annotations but its layout is unknown (标注格式不支持) */
            supported: boolean;
            reason: string | null;
            /** @description the track the subtitle bar shows first */
            primary: boolean;
        };
        VizFieldNode: {
            id: string;
            name: string;
            /** @enum {unknown} */
            kind: "group" | "camera" | "series" | "depth" | "text" | "table" | "file" | "topic" | "metadata" | "attachment" | "other";
            dtype?: string | null;
            shape?: number[] | null;
            names?: string[] | null;
            /** @description attributes shown in the detail pane */
            detail?: {
                [key: string]: string | number | boolean | null;
            };
            camera?: components["schemas"]["VizCameraKey"];
            /** @description the VizStream it is drawn as */
            stream?: string;
            /** @description a metadata file getDatasetVizMeta can read */
            file?: string;
            children?: components["schemas"]["VizFieldNode"][];
        };
        VizMappingState: {
            /**
             * @description not_needed: not mcap; none: an mcap dataset without a confirmed mapping; frozen: a task's (run.json)
             * @enum {unknown}
             */
            state: "not_needed" | "none" | "confirmed" | "frozen";
            version: number | null;
            name: string | null;
        };
        VizWarning: {
            code: string;
            /** @description Chinese */
            message: string;
        };
        VizDataset: {
            /** @enum {unknown} */
            scope: "dataset" | "task";
            /** @description the dataset id, or the task id for scope task */
            id: string;
            /** @description the registration; for scope task the task's, while it exists */
            dataset_id: string | null;
            name: string;
            format: components["schemas"]["VizFormat"];
            /** @description null for mcap: every topic keeps its own rate */
            fps: number | null;
            episode_count: number;
            /** @description every index in the grammar of --episodes when they are not 0 .. count-1 */
            episode_indices: string | null;
            total_frames: number | null;
            robot_type: string | null;
            bytes?: number | null;
            cameras: components["schemas"]["VizCamera"][];
            streams: components["schemas"]["VizStream"][];
            annotation_sources: components["schemas"]["VizAnnotationSource"][];
            field_tree: components["schemas"]["VizFieldNode"][];
            mapping: components["schemas"]["VizMappingState"];
            transcode: {
                /** @description CURATOR_VIZ_TRANSCODE */
                enabled: boolean;
            };
            warnings: components["schemas"]["VizWarning"][];
            /** @description the metadata fingerprint the model and its caches belong to */
            fingerprint: string;
        };
        VizEpisodeItem: {
            index: number;
            duration_s: number | null;
            frames: number | null;
            /** @description empty when there is none */
            task: string;
            /** @description the primary track's segments when the episode table has them (not read from the frames) */
            steps?: {
                start_s: number;
                end_s: number;
                unqualified?: boolean;
            }[] | null;
        };
        VizEpisodePage: components["schemas"]["CursorPage"] & {
            items?: components["schemas"]["VizEpisodeItem"][];
            /** @description matches of the filter */
            total: number;
        };
        VizTimeline: {
            /**
             * @description frame: frame = round(t x fps); timestamp: frame k sits at frame_times[k]
             * @enum {unknown}
             */
            kind: "frame" | "timestamp";
            fps: number | null;
            /** @description mcap: the topic whose messages number the frames */
            frame_reference: string | null;
            /** @description mcap: the episode time of every frame */
            frame_times: number[] | null;
        };
        /** @description the checks' clock on the episode clock (task scope): t = offset_s + time_s, t = offset_s + frame / fps */
        VizCheckClock: {
            offset_s: number;
            fps: number | null;
        };
        VizEpisodeCamera: {
            key: components["schemas"]["VizCameraKey"];
            /** @enum {unknown} */
            kind: "video" | "frames";
            access: components["schemas"]["VizAccess"];
            /** @description video: what <video> plays; frames: the frame pack; null when unsupported */
            url: string | null;
            /** @description frames: the frame pack's index; with samples_url, the sample pack's (VizFrameIndex with key and codec_string) */
            index_url: string | null;
            /**
             * @description 2.5.0 (design doc 19 §3): an mcap H.264 / H.265 camera's sample pack - one Annex-B access
             *     unit per message back to back as the recording has them, indexed from the first keyframe
             *     with the stream's parameter sets (`VizFrameIndex.config`) - for the browser to decode
             *     itself (WebCodecs); it falls back to `url`. Null when CURATOR_VIZ_CLIENT_DECODE is off,
             *     for any other camera, or a stream without a keyframe, without parameter sets or with B
             *     slices.
             */
            samples_url: string | null;
            /** @description the H.264 transcode to fall back to when the browser cannot decode url; null when transcoding is off or url already is one */
            transcode_url: string | null;
            /** @description LeRobot v3: where the episode starts in the file (play url#t=from_ts,to_ts) */
            from_ts: number | null;
            to_ts: number | null;
            /** @description episode time of the camera's first frame (video time 0 after from_ts) */
            offset_s: number;
            transcoded: boolean;
            /** @description epoch ms when a presigned url stops working */
            expires_at: number | null;
            reason: string | null;
        };
        VizSegment: {
            start_s: number;
            end_s: number;
            label: string;
            /** @enum {unknown} */
            quality?: "qualified" | "unqualified" | null;
            /** @description Argus: advancing / wasteful / idle */
            contribution?: string | null;
            arm?: string | null;
            /** @description boolean segment columns that hold (is_intervention_segment ...) */
            flags?: string[];
        };
        VizTrack: {
            key: string;
            name: string;
            source: string;
            primary: boolean;
            segments: components["schemas"]["VizSegment"][];
        };
        VizEvent: {
            t_s: number;
            label: string;
            outcome?: string | null;
            source: string;
        };
        VizLabel: {
            key: string;
            name: string;
            value: string;
            source: string;
        };
        VizAnnotations: {
            tracks: components["schemas"]["VizTrack"][];
            events: components["schemas"]["VizEvent"][];
            /** @description episode labels: success, rating, task_status ... */
            labels: components["schemas"]["VizLabel"][];
            warnings: components["schemas"]["VizWarning"][];
        };
        VizEpisode: {
            /** @enum {unknown} */
            scope: "dataset" | "task";
            id: string;
            index: number;
            duration_s: number;
            frames: number;
            fps: number | null;
            task: null | {
                text: string;
                /** @description 原始标注 / 人工改标 / 自产描述 */
                source: string;
            };
            timeline: components["schemas"]["VizTimeline"];
            cameras: components["schemas"]["VizEpisodeCamera"][];
            annotations: components["schemas"]["VizAnnotations"];
            check_clock: null | components["schemas"]["VizCheckClock"];
            warnings: components["schemas"]["VizWarning"][];
        };
        VizSeries: {
            stream: string;
            unit: string | null;
            from_s: number;
            to_s: number;
            /** @description episode seconds, shared by every line */
            t: number[];
            lines: {
                name: string;
                /** @enum {unknown} */
                role: "state" | "action" | "other";
                values: (number | null)[];
            }[];
            /** @description raw samples in the window */
            total_points: number;
            downsampled: boolean;
        };
        VizFrameIndex: {
            camera: components["schemas"]["VizCameraKey"];
            /**
             * @description h264 / h265: a sample pack (2.5.0)
             * @enum {unknown}
             */
            codec: "jpeg" | "png" | "h264" | "h265";
            width: number | null;
            height: number | null;
            count: number;
            /** @description episode seconds of every frame */
            t: number[];
            /** @description sample packs: whether each frame is a keyframe (decoding starts at one); 2.5.0 */
            key?: boolean[];
            /** @description sample packs: RFC 6381 codec string from the stream's own parameter sets (avc1.PPCCLL, hvc1....), for VideoDecoder.configure; 2.5.0 */
            codec_string?: string | null;
            /** @description sample packs: the stream's parameter sets (H.264 SPS / PPS, H.265 VPS / SPS / PPS) as Annex-B, base64 - put in front of the keyframe decoding starts at, since a recording may send them only once; 2.5.0 */
            config?: string | null;
            /** @description byte offset of every frame in the pack */
            offset: number[];
            size: number[];
            /** @description the pack's size */
            bytes: number;
        };
        VizMediaPending: {
            /** @enum {unknown} */
            state: "pending" | "failed";
            progress: number | null;
            message: string;
        };
        VizMetaFile: {
            path: string;
            size: number;
            truncated: boolean;
            /** @enum {unknown} */
            kind: "json" | "jsonl" | "text" | "markdown";
            text: string;
        };
        VizMapping: components["schemas"]["viz-mapping.schema"];
        DatasetMapping: {
            dataset_id: string;
            /** @enum {unknown} */
            state: "none" | "confirmed";
            mapping: null | components["schemas"]["viz-mapping.schema"];
            /** @description 0 = never confirmed */
            version: number;
            updated_at: number | null;
            /** @description the check reader's ingest.mcap_mapping derived from it (read-only, design doc 18 §6.2) */
            check_mapping: Record<string, unknown> | null;
            warnings: components["schemas"]["VizWarning"][];
        };
        DatasetMappingPut: {
            mapping: components["schemas"]["viz-mapping.schema"];
        };
        DatasetMappingInfo: {
            /** @enum {unknown} */
            state: "none" | "confirmed";
            version: number;
            updated_at: number | null;
            name: string | null;
        };
        DatasetAnnotationsInfo: {
            upload_id: components["schemas"]["UploadId"];
            name: string;
            /** @description argus */
            format: string;
            episodes: number;
            uploaded_at: number;
        };
        DatasetAnnotationsPut: {
            upload_id: null | components["schemas"]["UploadId"];
        };
        McapProbeRequest: {
            input: components["schemas"]["InputSpec"];
            /** @description an episode file to probe instead of the first */
            file?: string;
            template?: components["schemas"]["VizTemplateId"];
        };
        McapTopic: {
            topic: string;
            schema: string | null;
            schema_encoding: string | null;
            message_encoding: string | null;
            count: number | null;
            rate_hz: number | null;
            /** @description first message, seconds after the file's first message */
            start_s: number | null;
            end_s: number | null;
            image: null | {
                codec: string;
                width: number | null;
                height: number | null;
            };
            /** @description the numeric leaves of its first message */
            fields: {
                path: string;
                size: number;
            }[] | null;
            /**
             * @description what the draft does with it
             * @enum {unknown}
             */
            use: "camera" | "series" | "task" | "segments" | "ignore" | "unmapped";
            /** @enum {unknown} */
            role: "state" | "action" | "other" | null;
            name: string;
            /** @description Chinese: high rate, unsupported encoding ... */
            notes: string[];
        };
        McapProbe: {
            file: string;
            /** @description episode files in the dataset */
            files: number;
            topics: components["schemas"]["McapTopic"][];
            /** @description metadata records, by name, then key */
            metadata: {
                [key: string]: {
                    [key: string]: string;
                };
            };
            attachments: {
                name: string;
                media_type: string;
                size: number;
            }[];
            draft: components["schemas"]["viz-mapping.schema"];
            matched: null | {
                template_id: components["schemas"]["VizTemplateId"];
                name: string;
                coverage: number;
            };
            warnings: components["schemas"]["VizWarning"][];
        };
        VizTemplateId: string;
        VizTemplate: {
            id: components["schemas"]["VizTemplateId"];
            name: string;
            description: string;
            builtin: boolean;
            /** @description null for the built-ins, which are rules applied to a probe (design doc 18 §6.3), not a fixed mapping */
            mapping: null | components["schemas"]["viz-mapping.schema"];
            created_at: number | null;
            updated_at: number | null;
        };
        VizTemplateCreate: {
            name: string;
            description?: string;
            mapping: components["schemas"]["viz-mapping.schema"];
        };
        topic: string;
        /** @description a dotted path into a decoded message: field names, list indexes (joints.0) and * for every element of a list or every field of a message */
        path: string;
        camera: {
            topic: components["schemas"]["topic"];
            name: string;
            /** @description the schema name seen when it was mapped (informational; encoding and picture size come from probing) */
            schema?: string;
        };
        series: {
            topic: components["schemas"]["topic"];
            name: string;
            schema?: string;
            /** @description the numbers to draw, flattened in order (a message field expands to its numeric leaves); absent or empty = the whole message read as a vector by its shape (position, data, a bare list), as the check reader does */
            fields?: components["schemas"]["path"][];
            /** @description one name per flattened number; the check reader's action_names / state names come from these. Absent = from names_field, else <topic>_<i> */
            labels?: string[];
            /** @description a field holding the dimension names in every message (sensor_msgs/JointState.name) */
            names_field?: components["schemas"]["path"];
            /** @description display-only conversions of fields named in fields (a quaternion becomes roll, pitch, yaw in radians); the check reader always reads the raw numbers */
            transforms?: {
                [key: string]: "quat_xyzw_to_rpy" | "quat_wxyz_to_rpy" | "deg_to_rad" | "rad_to_deg";
            };
            /** @description the y axis unit (m, rad, N, ...) */
            unit?: string | null;
            /**
             * @description solid lines for state, dashed for action; also what the check reader takes it for
             * @enum {unknown}
             */
            role: "state" | "action" | "other";
            /** @description the series of the opposite role drawn in the same group (/left-arm-action with /left-arm-state) */
            pair_with?: null | components["schemas"]["topic"];
            /**
             * @description shown by the smart layout; false = only offered in the + / 更换 menus (high-rate or auxiliary curves)
             * @default true
             */
            smart?: boolean;
        };
        /**
         * viz-mapping/1.0 - the field mapping of an mcap dataset (C7, design doc 18 §6)
         * @description Which topics of an mcap dataset are cameras, which are curves and what they are (state / action / other), where the task text and the segment annotations are, and what numbers the frames. It is the configuration of the mcap reader of the data visualizer and, frozen into run.json when a task starts, the source of the check reader's ingest.mcap_mapping (design doc 18 §6.2: role=action series -> action, role=state -> state, cameras -> video_topics; transforms are for display only). A dataset keeps one (Dataset.viz_mapping, versioned); the built-in templates draft it from a probe and a person confirms it; it can also be imported, exported and saved as a site template. Topics not named anywhere are 'unmapped' and only reported. Episode files are numbered by v1's rule (episode_<N>.mcap, otherwise by sorted name), not by the mapping.
         */
        "viz-mapping.schema": {
            /** @constant */
            schema_version: "viz-mapping/1.0";
            /** @description shown in the template picker and on the dataset page */
            name?: string;
            /**
             * @description the built-in template it was drafted from; null = written by hand or imported. builtin:umi also sets the check reader's embodiment profile (umi_das)
             * @enum {unknown}
             */
            base?: "builtin:foxglove" | "builtin:ros2" | "builtin:umi" | null;
            timeline?: {
                /**
                 * @description which time of a message places it on the episode clock; message_timestamp reads timestamp_field of every message
                 * @default log_time
                 * @enum {unknown}
                 */
                source?: "log_time" | "publish_time" | "message_timestamp";
                /** @description the message's own time field (seconds, or a {sec, nsec} / {seconds, nanos} message), for source=message_timestamp */
                timestamp_field?: components["schemas"]["path"];
                /** @description the topic whose messages number the frames (the frame box of the player, design doc 18 §4.4); null = the first role=action series (the check reader's rows), else the first camera */
                frame_reference?: null | components["schemas"]["topic"];
            };
            /** @description in display order; compressed images (JPEG, PNG) and compressed video (H.264, H.265) */
            cameras: components["schemas"]["camera"][];
            /** @description curve groups, in display order; a state and an action topic of the same thing are paired with pair_with and drawn in one group */
            series: components["schemas"]["series"][];
            /** @description where the episode's task text is; null = none (the player shows 无任务描述) */
            task?: null | {
                /** @description a key of the file's metadata records (any record name) */
                metadata_key: string;
            } | {
                topic: components["schemas"]["topic"];
                /** @description the text field; absent = the message itself is the text (std_msgs/String data, a JSON string) */
                field?: components["schemas"]["path"];
            };
            /** @description where the segment annotations are; null = none (nothing is shown and nothing is warned about) */
            segments?: null | {
                topic: components["schemas"]["topic"];
                start_field: components["schemas"]["path"];
                end_field: components["schemas"]["path"];
                label_field: components["schemas"]["path"];
            } | {
                /** @description the name of an mcap attachment holding Argus-style JSON (timeline / key_events / completion) */
                attachment: string;
            };
            /** @description topics left out on purpose (calibration, system info, high-rate IMU ...); other unnamed topics are reported as unmapped */
            ignore?: components["schemas"]["topic"][];
        };
        module_id: string;
        draft_note: {
            code: string;
            /** @description The part of the draft it is about (record_mapping: pose | joints) */
            source?: string;
            args?: Record<string, unknown>;
        };
        param_draft: {
            /** @description The drafted parameter file (for record_mapping an eef-mapping/1.1 mapping); null when nothing could be drafted */
            document: Record<string, unknown> | null;
            /** @description What the draft infers rather than reads, each to be shown to the person confirming it */
            assumptions: components["schemas"]["draft_note"][];
            /** @description The parts left out, and why */
            not_drafted: components["schemas"]["draft_note"][];
        };
        /** @constant */
        schema_version: "1.0";
        module_availability: {
            id: components["schemas"]["module_id"];
            /** @enum {unknown} */
            availability: "available" | "needs_input" | "unsupported";
            /** @description English, for the terminal and logs; UIs render reason_code instead */
            reason?: string;
            /** @description Stable reason for UIs to translate. Known codes: format_unsupported {detected}, format_disabled {format} (mcap / lance switched off by the site: ingest.mcap_enabled / ingest.lance_enabled, D44), format_unsupported_by_module {format} (a module that cannot read this format: EEF-video consistency on lance; it reads mcap image topics since F5.13), metadata_invalid {problem}, missing_input {missing: [timestamps|action|state|video], video_cause?: none_declared|files_missing}, embodiment_unsupported {subject, given_by: robot_type|embodiment_id, supported}, robot_type_unknown {robot_type}, vlm_backend_missing; EEF-video consistency (design doc 12 §5.1): trajectory_missing {path?}, trajectory_invalid {errors, sha256}, observation_seed_missing (as the module's reason: neither observation seeds nor a gripper template, nothing finds the gripper in the video), eef_review_not_available (before F5.6), eef_base_unavailable {base_reason_code} (the review: the module it reviews is unusable), and as sub-item reasons projection_missing, observation_seed_missing and the rest of that catalogue. New modules may add codes; a UI that does not know one shows reason. */
            reason_code?: string;
            /** @description parameters of reason_code, listed with each code */
            reason_args?: Record<string, unknown>;
            input_hint?: {
                /**
                 * @description trajectory_json (C1 1.5): the module's upload parameter of that name; observation_seeds: the EEF module has neither observation seeds nor a gripper template (either one will do)
                 * @enum {unknown}
                 */
                field: "embodiment_id" | "vlm" | "trajectory_json" | "observation_seeds";
                options?: string[];
            };
            notes?: string[];
            /** @description Per sub-item capability of a module that has sub-items (EEF-video consistency, design doc 12 §5.1): the best availability of each sub-item over the selected episodes, with a reason when none is available */
            subitems?: {
                [key: string]: {
                    /** @enum {unknown} */
                    availability: "available" | "needs_input" | "unsupported";
                    reason_code: string | null;
                };
            };
            /** @description Modules with per-episode inputs (EEF-video consistency): the selected episodes by availability */
            episode_counts?: {
                [key: string]: number;
            };
            /** @description Parameters the module drafts from the dataset's metadata for a person to confirm before use, by parameter key (design doc 12 §8.7, D-E17: EEF-video consistency drafts record_mapping from a LeRobot info.json) */
            drafts?: {
                [key: string]: components["schemas"]["param_draft"];
            };
        } & (unknown & unknown);
        digest: string;
        /**
         * curation preflight --json (schema 1.0)
         * @description Design doc 02, section 3.1; availability rules in doc 05, section 4. Reads metadata only.
         */
        "preflight.schema": {
            schema_version: components["schemas"]["schema_version"];
            format: {
                /**
                 * @description lerobot, mcap and lance (lerobot-lance-convert >= 0.3.0, D44) can be supported; lancedb (other Lance tables), rrd and unknown never are
                 * @enum {unknown}
                 */
                kind: "lerobot" | "mcap" | "lance" | "lancedb" | "rrd" | "unknown";
                /**
                 * @description the LeRobot version of the metadata: v2 / v3 for lerobot, v3 for lance (its meta/ is LeRobot v3.0), null for mcap
                 * @enum {unknown}
                 */
                version: "v2" | "v3" | null;
                /** @description false => every module is unsupported */
                supported: boolean;
                detail: string;
            };
            /** @description info.json problems, shown to the user verbatim; non-empty => supported=false */
            validation: string[];
            dataset: null | {
                /** @description how many episodes; their indices are 0 .. count-1 unless episode_indices says otherwise */
                episode_count: number;
                /** @description only when the indices are not 0 .. count-1 (a subset that keeps its source's numbers, mcap files named episode_<N>.mcap): every index, in the grammar of --episodes (5,9,12-20). Selections - the plan, the Daemon's all / first N / explicit, the console's form - pick from these */
                episode_indices?: string;
                /** @description short camera names: the video feature key without the observation.images. prefix */
                cameras: string[];
                /** @description null for mcap: its time axis is the action topic's log_time */
                fps: number | null;
                robot_type: string | null;
                total_frames: number | null;
                labels: {
                    with_task: number;
                    without_task: number;
                };
                profile: null | {
                    matched: string;
                    by: string;
                };
                /** @description LeRobot (design doc 18 §7, F13.1): every feature of info.json as declared, for the visualizer's field tree; names flattened to a list (a {"motors": [...]} dict gives its list), null when info.json has none */
                features?: {
                    key: string;
                    dtype: string;
                    shape: number[] | null;
                    names: string[] | null;
                }[];
                /** @description one per entry of cameras, same order and names: picture size, codec and fps from info.json's video info (mcap: from the first frame), and whether no browser plays the codec so the visualizer serves an H.264 transcode (design doc 18 §4.2, D60) */
                camera_info?: {
                    /** @description the short name, as in cameras */
                    name: string;
                    /** @description the feature key, or the mcap topic */
                    key: string;
                    codec: string | null;
                    pix_fmt?: string | null;
                    width: number | null;
                    height: number | null;
                    fps: number | null;
                    needs_transcode: boolean;
                }[];
                /** @description segment annotations the visualizer recognises (design doc 18 §4.5, D64); supported false = something looks like annotations in a layout it does not know (标注格式不支持) */
                segment_sources?: {
                    /** @enum {unknown} */
                    kind: "segments" | "events" | "labels";
                    format: string;
                    name: string;
                    source: string;
                    supported: boolean;
                    reason?: string | null;
                }[];
                /** @description mcap: the topics in the first episode file's summary; which of them are cameras and curves is the dataset's field mapping (C7) */
                topics?: {
                    topic: string;
                    schema: string | null;
                    message_encoding: string | null;
                    count: number | null;
                    rate_hz: number | null;
                }[];
            };
            modules: components["schemas"]["module_availability"][];
            /** @description the listing fingerprint (source-manifest summary.digest) computed over the metadata objects only */
            meta_fingerprint: components["schemas"]["digest"];
            warnings: string[];
        };
        /**
         * @description a document C2 2.0 changed, as tasks made since write it; its 1.0 form stays readable for tasks made before (D59)
         * @constant
         */
        schema_version_2: "2.0";
        limit: {
            value: number;
            /** @enum {unknown} */
            bound_by: "task" | "model" | "backend" | "site" | "planner" | "running_tasks";
        };
        module_list: components["schemas"]["module_id"][];
        /** @description which episode set a stage consumes; 2.0 plans use only selected and unlabeled */
        episodes_ref: string;
        /** @description Concurrency gates of a stage. The eight named ones are v1's; a new module may declare its own, named like a module id. */
        gates: {
            episode?: number;
            probe?: number;
            endstate?: number;
            arbitration?: number;
            guard_caption?: number;
            caption?: number;
            llm?: number;
            audit?: number;
        } & {
            [key: string]: number;
        };
        merge: {
            /** @enum {unknown} */
            strategy: "none" | "per_episode_multi_module";
            groups: {
                modules: components["schemas"]["module_list"];
                frame_policy: string;
            }[];
        };
        stage: {
            /** @description 1.0: autolabel, integrity, numeric, frame, vlm, verdict, dedup, profile_vlm, final; 2.0: the registry's stages (integrity, numeric, frame, dedup; autolabel, vlm, profile) and final; stages of new modules follow the same pattern */
            id: string;
            /** @enum {unknown} */
            kind: "cpu" | "vlm" | "aggregate";
            /** @enum {unknown} */
            command?: "autolabel" | "check" | "aggregate";
            modules?: components["schemas"]["module_list"];
            episodes?: components["schemas"]["episodes_ref"];
            concurrency?: number;
            /** @description 1.0 only: the stage's hard gates */
            hard_gates?: components["schemas"]["module_list"];
            gates?: components["schemas"]["gates"];
            merge?: components["schemas"]["merge"];
            /** @enum {unknown} */
            phase?: "funnel" | "final";
            /**
             * @description 2.0: the block the stage runs in
             * @enum {unknown}
             */
            block?: "cpu" | "vlm";
            /** @description 2.0: the stage before it in its block; a block's first stage has none */
            after?: string;
            /** @description 2.0: needs the whole selection at once (dedup, profile) */
            full_set?: boolean;
        } & (unknown & unknown);
        stage_2: {
            /** @enum {unknown} */
            episodes?: "selected" | "unlabeled";
        } & unknown;
        stage_1: unknown;
        plan_2: {
            schema_version: components["schemas"]["schema_version_2"];
            /** @description the effective N the VLM gates are derived from */
            vlm_parallelism: number;
            /** @description The upper bounds the plan honoured, and where each came from. */
            limits: {
                /** @description The most CPU workers this task may use: every core but two (planner) or the task's cap (task), whichever is lower (P4, D54). Plans made before D54 may say site. Under the Daemon, the workers of all running tasks share one pool of that many slots. */
                cpu_concurrency: components["schemas"]["limit"];
                vlm_parallelism: components["schemas"]["limit"];
            };
            stages: (components["schemas"]["stage"] & components["schemas"]["stage_2"])[];
            estimates: {
                vlm_requests: number;
                wall_clock_s: number;
                notes: string[];
            };
        };
        plan_1: {
            schema_version: components["schemas"]["schema_version"];
            /** @description the effective N the VLM gates are derived from */
            vlm_parallelism: number;
            /** @description The upper bounds the plan honoured, and where each came from. */
            limits: {
                /** @description The most CPU workers this task may use: every core but two (planner) or the task's cap (task), whichever is lower (P4, D54). Plans made before D54 may say site. Under the Daemon, the workers of all running tasks share one pool of that many slots. */
                cpu_concurrency: components["schemas"]["limit"];
                vlm_parallelism: components["schemas"]["limit"];
            };
            stages: (components["schemas"]["stage"] & components["schemas"]["stage_1"])[];
            estimates: {
                vlm_requests: number;
                wall_clock_s: number;
                notes: string[];
            };
        };
        /**
         * curation plan --json / plan.json (schema 2.0; 1.0 still read)
         * @description The execution plan the planner derives (design doc 04, section 3). It is data: the Daemon stores it with the task, schedules by it and serves it read-only. Callers can only lower the limits it is derived from (D31). 2.0 (design doc 17 §3.3): two blocks - every check stage names its block (cpu / vlm) and the stage before it in that block (after); full-set stages (dedup, profile) start once the earlier stages of their block are done; every stage reads the task's selection, so there are no survivors and no hard gates. 1.0 is the funnel's plan, still read for tasks made before (D59).
         */
        "plan.schema": {
        } & (components["schemas"]["plan_2"] | components["schemas"]["plan_1"]);
        revision: number;
        /** @description a code of the module's catalogue in the registry (C1 2.0) */
        finding_code: string;
        /** @description an item of the taxonomy (C6), e.g. FILE-3 */
        item_id: string;
        token_usage: {
            prompt: number;
            completion: number;
            reasoning: number;
            cached: number;
            requests: number;
            requests_unknown_usage: number;
        };
        overview_2: {
            dataset: Record<string, unknown>;
            run: Record<string, unknown>;
            counts: {
                total: number;
                passed: number;
                rejected: number;
                held: number;
                review: number;
                /** @description episodes left out because source files are missing (D40); not part of total */
                skipped?: number;
            };
            /** @description passed / total; held is neither */
            pass_rate: number | null;
            /** @description rejected episodes per blocking reason: a finding (module, code, item), a person or a duplicate; an episode with several blocking findings counts under each */
            reject_reasons: {
                module: components["schemas"]["module_id"];
                /** @enum {unknown} */
                kind: "finding" | "human" | "duplicate";
                code?: components["schemas"]["finding_code"];
                item?: components["schemas"]["item_id"] | null;
                count: number;
            }[];
            token_usage: components["schemas"]["token_usage"];
            duration_s: number | null;
            /** @description the policy the verdicts were computed with (frozen at start; a copy is policy.json in the revision) */
            policy: {
                preset: string;
                version?: string;
            };
            /** @description which taxonomy items this task's modules cover (design doc 17 §5.2, the coverage matrix) */
            coverage: {
                taxonomy_version: string;
                covered: components["schemas"]["item_id"][];
                not_covered: components["schemas"]["item_id"][];
                unassessable: {
                    item: components["schemas"]["item_id"];
                    reason: string;
                    count: number;
                }[];
            };
            /** @description rejected episodes per taxonomy item of their blocking reasons, every module together - an episode counts once per item (the overview's reject reasons by item, F12.5); item null: a reason without a finding code (a person's discard) */
            reject_items?: {
                item: components["schemas"]["item_id"] | null;
                count: number;
            }[];
            /** @description episodes with a finding of each item, every module together, split by the level the policy gave */
            findings_by_item: {
                item: components["schemas"]["item_id"] | null;
                episodes: number;
                blocking: number;
                review: number;
                info: number;
            }[];
        };
        /** @enum {unknown} */
        finding_level: "blocking" | "review" | "info";
        /** @enum {unknown} */
        severity: "high" | "medium" | "low";
        /** @description what a finding is about; none means the whole episode. Cameras by their short name, as the expectations write them */
        scope: {
            camera?: string;
            cameras?: string[];
            channel?: string;
            arm?: string;
            clock?: string;
            file?: string;
        };
        /** @description one problem a module found (design doc 17 §1.2). Its level is not the module's: a policy gives it in aggregate */
        finding: {
            code: components["schemas"]["finding_code"];
            /** @description the item the registry maps the code to; null only for an info-level item of the platform's own */
            item: components["schemas"]["item_id"] | null;
            severity: components["schemas"]["severity"];
            scope?: null | components["schemas"]["scope"];
            /** @description [first, last] frame, counted from the episode's first, both included (P19) */
            frames?: [
                number,
                number
            ];
            /** @description [start, end) seconds from the episode's start */
            time_s?: [
                number,
                number
            ];
            /** @description the numbers behind the finding */
            readings?: Record<string, unknown>;
            /** @description one Chinese sentence with camera, position and numbers; shown as is */
            message_zh: string;
            /** @description evidence files, relative to the run directory */
            evidence?: string[];
            /**
             * @description a dataset-level finding (checks/<module>/dataset.json): counted once per dataset
             * @constant
             */
            unit?: "dataset";
        };
        module_section_2: {
            id: components["schemas"]["module_id"];
            /** @enum {unknown} */
            state: "succeeded" | "completed_with_errors" | "failed";
            /** @description the 1.0 keys of the module's own views stay (design doc 06 §6.2); 2.0 adds the generic statistics every section has */
            summary: {
                /** @description episodes the module judged (status ok) */
                assessed_episodes: number;
                /** @description of them, the episodes with at least one finding of the module (F12.5) */
                flagged_episodes?: number;
                /** @description the episodes with at least one finding of the module the revision's policy grades blocking / review / info (F12.5) */
                levels?: {
                    blocking: number;
                    review: number;
                    info: number;
                };
                /** @description per finding code: the episodes with it and their share of the assessed ones */
                items: {
                    item: components["schemas"]["item_id"] | null;
                    code: components["schemas"]["finding_code"];
                    level: components["schemas"]["finding_level"];
                    episodes: number;
                    share: number | null;
                    by_camera?: {
                        camera: string;
                        episodes: number;
                    }[];
                }[];
                unassessable: {
                    reason: string;
                    count: number;
                    item?: components["schemas"]["item_id"];
                }[];
                /** @description sub-item -> histogram of its readings */
                score_hist?: Record<string, unknown>;
                /** @description the module's dataset-level findings (checks/<module>/dataset.json) */
                dataset_findings?: components["schemas"]["finding"][];
            };
            tables: {
                id: string;
                rows: number;
                /** @description tables/<id>.parquet, sorted by episode_index */
                file: string;
            }[];
            adjudication: null | {
                /** @description open questions this module raised that must be decided */
                pending: number;
                /** @description rejects attributed to this module a person may appeal (D42); not pending */
                appealable?: number;
            };
            episodes_error?: number;
            error?: string;
            /** @description code / prompt hashes when the result came from a subtask (design doc 06, section 2) */
            fingerprints?: Record<string, unknown>;
        } & unknown;
        skipped_modules: {
            id: components["schemas"]["module_id"];
            reason: string;
        }[];
        episode_index: number;
        /** @description episodes left out because source files are missing (D40, as v1 does): never read, no result line, in none of the lists, not part of total */
        skipped_episodes: {
            episode_index: components["schemas"]["episode_index"];
            /** @description the missing object keys, relative to the input */
            missing: string[];
        }[];
        /** @description data package integrity: format, missing fields, unlabeled count, semantics profile / action semantics preflight */
        integrity: {
            /** @description what was not checked and why: the source manifest's list plus any found at read time */
            skipped_episodes?: components["schemas"]["skipped_episodes"];
        } & {
            [key: string]: unknown;
        };
        /** @description summary; the full profile is perf.json in the same revision */
        perf: Record<string, unknown>;
        overview_1: {
            dataset: Record<string, unknown>;
            run: Record<string, unknown>;
            counts: {
                total: number;
                passed: number;
                rejected: number;
                held: number;
                review: number;
                /** @description episodes left out because source files are missing (D40); not part of total */
                skipped?: number;
            };
            /** @description passed / total; held is neither */
            pass_rate: number | null;
            reject_reasons: {
                module: components["schemas"]["module_id"];
                count: number;
            }[];
            token_usage: components["schemas"]["token_usage"];
            duration_s: number | null;
        };
        module_section_1: {
            id: components["schemas"]["module_id"];
            /** @enum {unknown} */
            state: "succeeded" | "completed_with_errors" | "failed";
            /** @enum {unknown} */
            gate: "hard" | "soft" | "dedup" | "none";
            summary: Record<string, unknown>;
            tables: {
                id: string;
                rows: number;
                /** @description tables/<id>.parquet, sorted by episode_index */
                file: string;
            }[];
            adjudication: null | {
                /** @description open questions this module raised that must be decided */
                pending: number;
                /** @description rejects attributed to this module a person may appeal (D42); not pending */
                appealable?: number;
            };
            episodes_error?: number;
            error?: string;
            /** @description code / prompt hashes when the result came from a subtask (design doc 06, section 2) */
            fingerprints?: Record<string, unknown>;
        } & unknown;
        report_2: {
            schema_version: components["schemas"]["schema_version_2"];
            revision: components["schemas"]["revision"];
            overview: components["schemas"]["overview_2"];
            modules: components["schemas"]["module_section_2"][];
            skipped_modules: components["schemas"]["skipped_modules"];
            integrity: components["schemas"]["integrity"];
            perf: components["schemas"]["perf"];
        };
        report_1: {
            schema_version: components["schemas"]["schema_version"];
            revision: components["schemas"]["revision"];
            overview: components["schemas"]["overview_1"];
            modules: components["schemas"]["module_section_1"][];
            skipped_modules: components["schemas"]["skipped_modules"];
            integrity: components["schemas"]["integrity"];
            perf: components["schemas"]["perf"];
        };
        /**
         * revisions/r<NNNN>/report.json (schema 2.0; 1.0 still read)
         * @description Report structure (design doc 06, section 6). modules[] follows the selected modules one to one, in registry order; a module that failed keeps its section with the error. URLs are not part of the file: the REST layer adds links. 2.0 (design doc 17 §5.1): the overview names the policy, the taxonomy coverage and the findings per item, rejects are counted per blocking finding, and every module section carries the generic per-code statistics; the gates are gone. The console tells the two apart by schema_version (D59).
         */
        "report.schema": {
        } & (components["schemas"]["report_2"] | components["schemas"]["report_1"]);
        /** @description an item the module covers but could not assess on this episode */
        unassessable: {
            item: components["schemas"]["item_id"];
            /** @description one of the registry's unassessable_reasons */
            reason: string;
            message_zh: string;
        };
        incident: {
            step: string;
            cause?: string;
            camera?: string;
            call_kind?: string;
            attempts?: number;
        };
        execution_error: {
            /** @constant */
            kind: "execution";
            incidents: components["schemas"]["incident"][];
        };
        record_2: {
            episode_index: components["schemas"]["episode_index"];
            module: components["schemas"]["module_id"];
            /**
             * @description error: the module could not judge this episode properly (D33); no findings, nothing assessed
             * @enum {unknown}
             */
            status: "ok" | "error";
            findings: components["schemas"]["finding"][];
            /** @description the items it covers minus the unassessable ones: an assessed item without a finding was checked and found clean */
            assessed: components["schemas"]["item_id"][];
            unassessable: components["schemas"]["unassessable"][];
            /** @description numbers that are no finding, for the report and the episode page */
            readings: Record<string, unknown>;
            /** @description module-specific, field names kept from v1 (parity and the episode page) */
            details: Record<string, unknown>;
            evidence: string[];
            elapsed_s: number | null;
            error: null | components["schemas"]["execution_error"];
        } & unknown;
        /** @description Record 1.0 (tasks made before C2 2.0, read only, D59): pass/fail/abstain/scored/error with the funnel gate. */
        record_1: {
            episode_index: number;
            module: string;
            /**
             * @description pass/fail: hard gate passed=True/False; abstain: passed=null and no score; scored: soft module with a score (never votes); error: the module could not judge this episode properly (D33).
             * @enum {unknown}
             */
            verdict: "pass" | "fail" | "abstain" | "scored" | "error";
            passed: boolean | null;
            score: number | null;
            /** @enum {unknown} */
            gate: "hard" | "soft" | "dedup" | "none";
            /** @description Module-specific, field names kept from v1. */
            details: Record<string, unknown>;
            evidence: string[];
            elapsed_s: number | null;
            error: null | {
                /** @constant */
                kind: "execution";
                incidents: {
                    step: string;
                    cause?: string;
                    camera?: string;
                    call_kind?: string;
                    attempts?: number;
                }[];
            };
        } & unknown;
        /**
         * Per-episode check result record (schema 2.0; 1.0 still read)
         * @description One line of checks/<module>/parts/*.jsonl (and of records/<module>.jsonl in a parity dump). 2.0 (design doc 17 §1.1, D56): the module's findings, the taxonomy items it assessed and those it could not, readings and the module's own details; no verdict, no score, no gate - whether the episode is rejected is the policy's call in aggregate. 1.0 is the funnel's record, still read for tasks made before (D59). Self-contained on purpose (no reference to common.schema.json): the parity tool validates records with this file alone; its definitions are common's, and a contract test keeps them equal.
         */
        "result-record.schema": {
        } & (components["schemas"]["record_2"] | components["schemas"]["record_1"]);
    };
    responses: {
        /**
         * @description 400 validation_failed, 401 unauthorized, 404 not_found, 409 task_state_conflict /
         *     subtask_active / credential_in_use / backend_in_use / name_taken / preflight_expired /
         *     source_changed / result_changed / idempotency_conflict, 412 precondition_failed,
         *     422 precheck_failed / confirm_path_mismatch / model_check_failed, 500 internal
         */
        Error: {
            headers: {
                [name: string]: unknown;
            };
            content: {
                "application/json": components["schemas"]["Error"];
            };
        };
        /** @description the whole video (Accept-Ranges bytes) */
        VizVideo: {
            headers: {
                [name: string]: unknown;
            };
            content: {
                "video/mp4": string;
            };
        };
        /** @description the requested byte range (Content-Range) */
        VizVideoRange: {
            headers: {
                [name: string]: unknown;
            };
            content: {
                "video/mp4": string;
            };
        };
        /** @description the whole frame pack (Accept-Ranges bytes) */
        VizFrames: {
            headers: {
                [name: string]: unknown;
            };
            content: {
                "application/octet-stream": string;
            };
        };
        /** @description the requested byte range of the frame pack (Content-Range) */
        VizFramesRange: {
            headers: {
                [name: string]: unknown;
            };
            content: {
                "application/octet-stream": string;
            };
        };
        /** @description still being prepared (transcoding, building the frame pack); ask again shortly */
        VizPending: {
            headers: {
                [name: string]: unknown;
            };
            content: {
                "application/json": components["schemas"]["VizMediaPending"];
            };
        };
    };
    parameters: {
        PathId: string;
        /** @description the same key within 24 hours returns the first response (doc 03 §8) */
        IdempotencyKey: string;
        /** @description opaque, from next_cursor */
        Cursor: string;
        Limit: number;
        /** @description result revision; omitted = the current one (task.result_rev) */
        Rev: number;
        Source: "tos" | "public" | "local";
        Region: components["schemas"]["Region"];
        /** @description access key name (not needed for source=public) */
        CredentialName: string;
        /** @description the episode's own index (preflight `episode_indices`) */
        PathIndex: number;
        /** @description the camera's `key` in the presentation model */
        PathCamera: components["schemas"]["VizCameraKey"];
        /** @description one byte range (`bytes=a-b`, `bytes=a-`, `bytes=-n`) */
        RangeHeader: string;
        /** @description true: the H.264 transcode of the camera, for a browser that cannot decode the original (AV1 without hardware decoding, HEVC); 404 with details.reason transcode_disabled when CURATOR_VIZ_TRANSCODE is off */
        VizTranscode: boolean;
        /** @description the curve group's `key` (VizStream.key) */
        VizStream: string;
        /** @description start of the window in episode seconds (default 0) */
        VizFrom: number;
        /** @description end of the window in episode seconds (default the episode's end) */
        VizTo: number;
        /** @description at most this many points per line; more raw points are thinned by min / max per bucket */
        VizPoints: number;
    };
    requestBodies: never;
    headers: never;
    pathItems: never;
}
export interface operations {
    listCredentials: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description all access keys of the owner */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": {
                        items: components["schemas"]["Credential"][];
                    };
                };
            };
            default: components["responses"]["Error"];
        };
    };
    createCredential: {
        parameters: {
            query?: never;
            header?: {
                /** @description the same key within 24 hours returns the first response (doc 03 §8) */
                "Idempotency-Key"?: components["parameters"]["IdempotencyKey"];
            };
            path?: never;
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["CredentialCreate"];
            };
        };
        responses: {
            /** @description saved */
            201: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["Credential"];
                };
            };
            default: components["responses"]["Error"];
        };
    };
    updateCredential: {
        parameters: {
            query?: never;
            header?: {
                /** @description the same key within 24 hours returns the first response (doc 03 §8) */
                "Idempotency-Key"?: components["parameters"]["IdempotencyKey"];
            };
            path: {
                id: components["parameters"]["PathId"];
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["CredentialUpdate"];
            };
        };
        responses: {
            /** @description updated */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["Credential"];
                };
            };
            default: components["responses"]["Error"];
        };
    };
    deleteCredential: {
        parameters: {
            query?: {
                confirm?: boolean;
            };
            header?: {
                /** @description the same key within 24 hours returns the first response (doc 03 §8) */
                "Idempotency-Key"?: components["parameters"]["IdempotencyKey"];
            };
            path: {
                id: components["parameters"]["PathId"];
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description deleted */
            204: {
                headers: {
                    [name: string]: unknown;
                };
                content?: never;
            };
            default: components["responses"]["Error"];
        };
    };
    verifyCredential: {
        parameters: {
            query?: never;
            header?: {
                /** @description the same key within 24 hours returns the first response (doc 03 §8) */
                "Idempotency-Key"?: components["parameters"]["IdempotencyKey"];
            };
            path: {
                id: components["parameters"]["PathId"];
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description verification result (also stored on the key) */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["VerifyResult"];
                };
            };
            default: components["responses"]["Error"];
        };
    };
    listVlmBackends: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description backends */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": {
                        items: components["schemas"]["VlmBackend"][];
                    };
                };
            };
            default: components["responses"]["Error"];
        };
    };
    createVlmBackend: {
        parameters: {
            query?: never;
            header?: {
                /** @description the same key within 24 hours returns the first response (doc 03 §8) */
                "Idempotency-Key"?: components["parameters"]["IdempotencyKey"];
            };
            path?: never;
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["VlmBackendCreate"];
            };
        };
        responses: {
            /** @description saved */
            201: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["VlmBackend"];
                };
            };
            default: components["responses"]["Error"];
        };
    };
    updateVlmBackend: {
        parameters: {
            query?: never;
            header?: {
                /** @description the same key within 24 hours returns the first response (doc 03 §8) */
                "Idempotency-Key"?: components["parameters"]["IdempotencyKey"];
            };
            path: {
                id: components["parameters"]["PathId"];
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["VlmBackendUpdate"];
            };
        };
        responses: {
            /** @description updated */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["VlmBackend"];
                };
            };
            default: components["responses"]["Error"];
        };
    };
    deleteVlmBackend: {
        parameters: {
            query?: {
                confirm?: boolean;
            };
            header?: {
                /** @description the same key within 24 hours returns the first response (doc 03 §8) */
                "Idempotency-Key"?: components["parameters"]["IdempotencyKey"];
            };
            path: {
                id: components["parameters"]["PathId"];
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description deleted */
            204: {
                headers: {
                    [name: string]: unknown;
                };
                content?: never;
            };
            default: components["responses"]["Error"];
        };
    };
    verifyVlmBackend: {
        parameters: {
            query?: never;
            header?: {
                /** @description the same key within 24 hours returns the first response (doc 03 §8) */
                "Idempotency-Key"?: components["parameters"]["IdempotencyKey"];
            };
            path: {
                id: components["parameters"]["PathId"];
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description result */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["VerifyResult"];
                };
            };
            default: components["responses"]["Error"];
        };
    };
    refreshVlmModels: {
        parameters: {
            query?: never;
            header?: {
                /** @description the same key within 24 hours returns the first response (doc 03 §8) */
                "Idempotency-Key"?: components["parameters"]["IdempotencyKey"];
            };
            path: {
                id: components["parameters"]["PathId"];
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description listing result */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": {
                        /** @description false when GET /models is not available */
                        listed: boolean;
                        models: components["schemas"]["VlmModel"][];
                        note?: string;
                    };
                };
            };
            default: components["responses"]["Error"];
        };
    };
    addVlmModel: {
        parameters: {
            query?: never;
            header?: {
                /** @description the same key within 24 hours returns the first response (doc 03 §8) */
                "Idempotency-Key"?: components["parameters"]["IdempotencyKey"];
            };
            path: {
                id: components["parameters"]["PathId"];
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["VlmModelCreate"];
            };
        };
        responses: {
            /** @description added */
            201: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["VlmModel"];
                };
            };
            default: components["responses"]["Error"];
        };
    };
    deleteVlmModel: {
        parameters: {
            query?: never;
            header?: {
                /** @description the same key within 24 hours returns the first response (doc 03 §8) */
                "Idempotency-Key"?: components["parameters"]["IdempotencyKey"];
            };
            path: {
                id: components["parameters"]["PathId"];
                model_id: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description removed */
            204: {
                headers: {
                    [name: string]: unknown;
                };
                content?: never;
            };
            default: components["responses"]["Error"];
        };
    };
    updateVlmModel: {
        parameters: {
            query?: never;
            header?: {
                /** @description the same key within 24 hours returns the first response (doc 03 §8) */
                "Idempotency-Key"?: components["parameters"]["IdempotencyKey"];
            };
            path: {
                id: components["parameters"]["PathId"];
                model_id: string;
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["VlmModelPatch"];
            };
        };
        responses: {
            /** @description updated */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["VlmModel"];
                };
            };
            default: components["responses"]["Error"];
        };
    };
    getModules: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description registry */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ModuleRegistry"];
                };
            };
            default: components["responses"]["Error"];
        };
    };
    getOverview: {
        parameters: {
            query?: {
                /** @description the period of `recent`: 7 or 30 days (by day), 90 = the 13 calendar weeks up to this one (by week), 365 = the 12 calendar months up to this one (by month); anything else is 400 */
                days?: 7 | 30 | 90 | 365;
            };
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description overview */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["Overview"];
                };
            };
            default: components["responses"]["Error"];
        };
    };
    listDatasets: {
        parameters: {
            query?: {
                page?: number;
                page_size?: 10 | 20 | 50 | 100;
                /** @description search by name or address */
                q?: string;
                format?: components["schemas"]["DatasetFormat"];
                check_state?: "ok" | "changed";
                /** @description true = only datasets a visualizer reader serves (LeRobot, mcap), for the visualize page's picker */
                viz?: boolean;
            };
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description one page */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": {
                        items: components["schemas"]["DatasetItem"][];
                        page: number;
                        page_size: number;
                        total: number;
                    };
                };
            };
            default: components["responses"]["Error"];
        };
    };
    createDataset: {
        parameters: {
            query?: never;
            header?: {
                /** @description the same key within 24 hours returns the first response (doc 03 §8) */
                "Idempotency-Key"?: components["parameters"]["IdempotencyKey"];
            };
            path?: never;
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["DatasetCreate"];
            };
        };
        responses: {
            /** @description already registered */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["DatasetDetail"];
                };
            };
            /** @description registered */
            201: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["DatasetDetail"];
                };
            };
            default: components["responses"]["Error"];
        };
    };
    browseDatasets: {
        parameters: {
            query: {
                source: components["parameters"]["Source"];
                /** @description tos:// prefix to list (source=tos) */
                uri?: string;
                region?: components["parameters"]["Region"];
                /** @description access key name (not needed for source=public) */
                credential?: components["parameters"]["CredentialName"];
                /** @description opaque, from next_cursor */
                cursor?: components["parameters"]["Cursor"];
                limit?: components["parameters"]["Limit"];
            };
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description datasets */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["CursorPage"] & {
                        items?: components["schemas"]["BrowsedDataset"][];
                    };
                };
            };
            default: components["responses"]["Error"];
        };
    };
    listDatasetEpisodes: {
        parameters: {
            query?: {
                dataset_id?: string;
                source?: "tos" | "public" | "local";
                uri?: string;
                region?: components["parameters"]["Region"];
                /** @description access key name (not needed for source=public) */
                credential?: components["parameters"]["CredentialName"];
                /** @description opaque, from next_cursor */
                cursor?: components["parameters"]["Cursor"];
                limit?: components["parameters"]["Limit"];
            };
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description episodes */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["CursorPage"] & {
                        items?: components["schemas"]["EpisodePreview"][];
                    };
                };
            };
            default: components["responses"]["Error"];
        };
    };
    getDataset: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                id: components["parameters"]["PathId"];
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description dataset */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["DatasetDetail"];
                };
            };
            default: components["responses"]["Error"];
        };
    };
    deleteDataset: {
        parameters: {
            query?: never;
            header?: {
                /** @description the same key within 24 hours returns the first response (doc 03 §8) */
                "Idempotency-Key"?: components["parameters"]["IdempotencyKey"];
            };
            path: {
                id: components["parameters"]["PathId"];
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description removed */
            204: {
                headers: {
                    [name: string]: unknown;
                };
                content?: never;
            };
            default: components["responses"]["Error"];
        };
    };
    updateDataset: {
        parameters: {
            query?: never;
            header?: {
                /** @description the same key within 24 hours returns the first response (doc 03 §8) */
                "Idempotency-Key"?: components["parameters"]["IdempotencyKey"];
            };
            path: {
                id: components["parameters"]["PathId"];
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["DatasetPatch"];
            };
        };
        responses: {
            /** @description updated */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["DatasetDetail"];
                };
            };
            default: components["responses"]["Error"];
        };
    };
    recheckDataset: {
        parameters: {
            query?: never;
            header?: {
                /** @description the same key within 24 hours returns the first response (doc 03 §8) */
                "Idempotency-Key"?: components["parameters"]["IdempotencyKey"];
            };
            path: {
                id: components["parameters"]["PathId"];
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description the check */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["DatasetCheck"];
                };
            };
            default: components["responses"]["Error"];
        };
    };
    repreflightDataset: {
        parameters: {
            query?: never;
            header?: {
                /** @description the same key within 24 hours returns the first response (doc 03 §8) */
                "Idempotency-Key"?: components["parameters"]["IdempotencyKey"];
            };
            path: {
                id: components["parameters"]["PathId"];
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description refreshed */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["DatasetDetail"];
                };
            };
            default: components["responses"]["Error"];
        };
    };
    signDataset: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                id: components["parameters"]["PathId"];
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["DatasetSignRequest"];
            };
        };
        responses: {
            /** @description one URL per request, in the same order */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["DatasetSignResponse"];
                };
            };
            default: components["responses"]["Error"];
        };
    };
    getDatasetViz: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                id: components["parameters"]["PathId"];
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description the dataset */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["VizDataset"];
                };
            };
            default: components["responses"]["Error"];
        };
    };
    listDatasetVizEpisodes: {
        parameters: {
            query?: {
                /** @description an episode number (`12`, `ep12`) or words of the task text */
                q?: string;
                /** @description `steps`: episodes without known steps first */
                sort?: "index" | "duration" | "steps";
                order?: "asc" | "desc";
                /** @description opaque, from next_cursor */
                cursor?: components["parameters"]["Cursor"];
                limit?: components["parameters"]["Limit"];
            };
            header?: never;
            path: {
                id: components["parameters"]["PathId"];
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description one page */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["VizEpisodePage"];
                };
            };
            default: components["responses"]["Error"];
        };
    };
    getDatasetVizMeta: {
        parameters: {
            query: {
                /** @description relative to the dataset, e.g. `meta/info.json` */
                path: string;
            };
            header?: never;
            path: {
                id: components["parameters"]["PathId"];
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description the file */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["VizMetaFile"];
                };
            };
            default: components["responses"]["Error"];
        };
    };
    getDatasetEpisodeViz: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                id: components["parameters"]["PathId"];
                /** @description the episode's own index (preflight `episode_indices`) */
                index: components["parameters"]["PathIndex"];
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description the episode */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["VizEpisode"];
                };
            };
            default: components["responses"]["Error"];
        };
    };
    getDatasetEpisodeSeries: {
        parameters: {
            query: {
                /** @description the curve group's `key` (VizStream.key) */
                stream: components["parameters"]["VizStream"];
                /** @description start of the window in episode seconds (default 0) */
                from?: components["parameters"]["VizFrom"];
                /** @description end of the window in episode seconds (default the episode's end) */
                to?: components["parameters"]["VizTo"];
                /** @description at most this many points per line; more raw points are thinned by min / max per bucket */
                points?: components["parameters"]["VizPoints"];
            };
            header?: never;
            path: {
                id: components["parameters"]["PathId"];
                /** @description the episode's own index (preflight `episode_indices`) */
                index: components["parameters"]["PathIndex"];
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description the curves */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["VizSeries"];
                };
            };
            default: components["responses"]["Error"];
        };
    };
    getDatasetCameraVideo: {
        parameters: {
            query?: {
                /** @description true: the H.264 transcode of the camera, for a browser that cannot decode the original (AV1 without hardware decoding, HEVC); 404 with details.reason transcode_disabled when CURATOR_VIZ_TRANSCODE is off */
                transcode?: components["parameters"]["VizTranscode"];
            };
            header?: {
                /** @description one byte range (`bytes=a-b`, `bytes=a-`, `bytes=-n`) */
                Range?: components["parameters"]["RangeHeader"];
            };
            path: {
                id: components["parameters"]["PathId"];
                /** @description the episode's own index (preflight `episode_indices`) */
                index: components["parameters"]["PathIndex"];
                /** @description the camera's `key` in the presentation model */
                camera: components["parameters"]["PathCamera"];
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            200: components["responses"]["VizVideo"];
            202: components["responses"]["VizPending"];
            206: components["responses"]["VizVideoRange"];
            /** @description the range is outside the file */
            416: {
                headers: {
                    [name: string]: unknown;
                };
                content?: never;
            };
            default: components["responses"]["Error"];
        };
    };
    getDatasetCameraFrames: {
        parameters: {
            query?: never;
            header?: {
                /** @description one byte range (`bytes=a-b`, `bytes=a-`, `bytes=-n`) */
                Range?: components["parameters"]["RangeHeader"];
            };
            path: {
                id: components["parameters"]["PathId"];
                /** @description the episode's own index (preflight `episode_indices`) */
                index: components["parameters"]["PathIndex"];
                /** @description the camera's `key` in the presentation model */
                camera: components["parameters"]["PathCamera"];
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            200: components["responses"]["VizFrames"];
            202: components["responses"]["VizPending"];
            206: components["responses"]["VizFramesRange"];
            /** @description the range is outside the pack */
            416: {
                headers: {
                    [name: string]: unknown;
                };
                content?: never;
            };
            default: components["responses"]["Error"];
        };
    };
    getDatasetCameraFrameIndex: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                id: components["parameters"]["PathId"];
                /** @description the episode's own index (preflight `episode_indices`) */
                index: components["parameters"]["PathIndex"];
                /** @description the camera's `key` in the presentation model */
                camera: components["parameters"]["PathCamera"];
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description the index */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["VizFrameIndex"];
                };
            };
            202: components["responses"]["VizPending"];
            default: components["responses"]["Error"];
        };
    };
    getDatasetMapping: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                id: components["parameters"]["PathId"];
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description the mapping */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["DatasetMapping"];
                };
            };
            default: components["responses"]["Error"];
        };
    };
    putDatasetMapping: {
        parameters: {
            query?: never;
            header?: {
                /** @description the same key within 24 hours returns the first response (doc 03 §8) */
                "Idempotency-Key"?: components["parameters"]["IdempotencyKey"];
            };
            path: {
                id: components["parameters"]["PathId"];
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["DatasetMappingPut"];
            };
        };
        responses: {
            /** @description confirmed */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["DatasetMapping"];
                };
            };
            default: components["responses"]["Error"];
        };
    };
    putDatasetAnnotations: {
        parameters: {
            query?: never;
            header?: {
                /** @description the same key within 24 hours returns the first response (doc 03 §8) */
                "Idempotency-Key"?: components["parameters"]["IdempotencyKey"];
            };
            path: {
                id: components["parameters"]["PathId"];
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["DatasetAnnotationsPut"];
            };
        };
        responses: {
            /** @description the dataset */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["DatasetDetail"];
                };
            };
            default: components["responses"]["Error"];
        };
    };
    preflight: {
        parameters: {
            query?: never;
            header?: {
                /** @description the same key within 24 hours returns the first response (doc 03 §8) */
                "Idempotency-Key"?: components["parameters"]["IdempotencyKey"];
            };
            path?: never;
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["PreflightRequest"];
            };
        };
        responses: {
            /** @description preflight result */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["PreflightResponse"];
                };
            };
            default: components["responses"]["Error"];
        };
    };
    probeDelivery: {
        parameters: {
            query?: never;
            header?: {
                /** @description the same key within 24 hours returns the first response (doc 03 §8) */
                "Idempotency-Key"?: components["parameters"]["IdempotencyKey"];
            };
            path?: never;
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["DeliveryProbeRequest"];
            };
        };
        responses: {
            /** @description probe result */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ProbeResult"];
                };
            };
            default: components["responses"]["Error"];
        };
    };
    createUpload: {
        parameters: {
            query: {
                kind: components["schemas"]["UploadKind"];
                /** @description the file name as picked (kept for display) */
                name: string;
            };
            header?: {
                /** @description the same key within 24 hours returns the first response (doc 03 §8) */
                "Idempotency-Key"?: components["parameters"]["IdempotencyKey"];
            };
            path?: never;
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": unknown;
                "application/zip": string;
            };
        };
        responses: {
            /** @description stored and validated */
            201: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["Upload"];
                };
            };
            default: components["responses"]["Error"];
        };
    };
    getUpload: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                upload_id: components["schemas"]["UploadId"];
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description the upload */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["Upload"];
                };
            };
            default: components["responses"]["Error"];
        };
    };
    probeMcap: {
        parameters: {
            query?: never;
            header?: {
                /** @description the same key within 24 hours returns the first response (doc 03 §8) */
                "Idempotency-Key"?: components["parameters"]["IdempotencyKey"];
            };
            path?: never;
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["McapProbeRequest"];
            };
        };
        responses: {
            /** @description the probe and the draft */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["McapProbe"];
                };
            };
            default: components["responses"]["Error"];
        };
    };
    listVizTemplates: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description templates */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": {
                        items: components["schemas"]["VizTemplate"][];
                    };
                };
            };
            default: components["responses"]["Error"];
        };
    };
    createVizTemplate: {
        parameters: {
            query?: never;
            header?: {
                /** @description the same key within 24 hours returns the first response (doc 03 §8) */
                "Idempotency-Key"?: components["parameters"]["IdempotencyKey"];
            };
            path?: never;
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["VizTemplateCreate"];
            };
        };
        responses: {
            /** @description saved */
            201: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["VizTemplate"];
                };
            };
            default: components["responses"]["Error"];
        };
    };
    deleteVizTemplate: {
        parameters: {
            query?: never;
            header?: {
                /** @description the same key within 24 hours returns the first response (doc 03 §8) */
                "Idempotency-Key"?: components["parameters"]["IdempotencyKey"];
            };
            path: {
                template_id: components["schemas"]["VizTemplateId"];
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description deleted */
            204: {
                headers: {
                    [name: string]: unknown;
                };
                content?: never;
            };
            default: components["responses"]["Error"];
        };
    };
    listTasks: {
        parameters: {
            query?: {
                page?: number;
                page_size?: 10 | 20 | 50 | 100;
                /** @description a task state, or `deleted` for soft-deleted tasks (restorable for 30 days). `running` also lists finished tasks whose subtask (retry, resume, adjudication run, re-export) is queued or running: the console shows them as running (D46); their `state` stays terminal and `active_subtask` names the subtask */
                state?: components["schemas"]["TaskState"] | "deleted";
                /** @description search by name or id */
                q?: string;
                /** @description tasks writing to this delivery directory */
                delivery?: string;
                /** @description comma-separated module ids; only tasks that selected every one of them (an unknown id is 400 validation_failed) */
                module?: string;
                /** @description tasks on this registered dataset */
                dataset_id?: string;
                /** @description true keeps only tasks with a committed result (result_rev >= 1, a report to read); false or absent does not filter (1.15) */
                has_result?: boolean;
                /** @description true keeps only tasks with pending adjudication items (TaskListItem.pending_adjudication > 0); false or absent does not filter (1.15) */
                pending_adjudication?: boolean;
            };
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description one page */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": {
                        items: components["schemas"]["TaskListItem"][];
                        page: number;
                        page_size: number;
                        total: number;
                    };
                };
            };
            default: components["responses"]["Error"];
        };
    };
    createTask: {
        parameters: {
            query?: never;
            header?: {
                /** @description the same key within 24 hours returns the first response (doc 03 §8) */
                "Idempotency-Key"?: components["parameters"]["IdempotencyKey"];
            };
            path?: never;
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["TaskCreate"];
            };
        };
        responses: {
            /** @description created (state queued, or created when start_now=false) */
            201: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["TaskCreated"];
                };
            };
            default: components["responses"]["Error"];
        };
    };
    createTasksBatch: {
        parameters: {
            query?: never;
            header?: {
                /** @description the same key within 24 hours returns the first response (doc 03 §8) */
                "Idempotency-Key"?: components["parameters"]["IdempotencyKey"];
            };
            path?: never;
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["TaskBatchCreate"];
            };
        };
        responses: {
            /** @description created */
            201: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": {
                        tasks: components["schemas"]["TaskCreated"][];
                    };
                };
            };
            default: components["responses"]["Error"];
        };
    };
    getTask: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                id: components["parameters"]["PathId"];
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description task detail (doc 03 §3.3) */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["Task"];
                };
            };
            default: components["responses"]["Error"];
        };
    };
    deleteTask: {
        parameters: {
            query?: never;
            header?: {
                /** @description the same key within 24 hours returns the first response (doc 03 §8) */
                "Idempotency-Key"?: components["parameters"]["IdempotencyKey"];
            };
            path: {
                id: components["parameters"]["PathId"];
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description deleted; restorable for 30 days */
            204: {
                headers: {
                    [name: string]: unknown;
                };
                content?: never;
            };
            default: components["responses"]["Error"];
        };
    };
    updateTask: {
        parameters: {
            query?: never;
            header: {
                /** @description the same key within 24 hours returns the first response (doc 03 §8) */
                "Idempotency-Key"?: components["parameters"]["IdempotencyKey"];
                /** @description the task's updated_at as returned by GET; a stale value returns 412 */
                "If-Match": string;
            };
            path: {
                id: components["parameters"]["PathId"];
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["TaskPatch"];
            };
        };
        responses: {
            /** @description updated */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["Task"];
                };
            };
            default: components["responses"]["Error"];
        };
    };
    restoreTask: {
        parameters: {
            query?: never;
            header?: {
                /** @description the same key within 24 hours returns the first response (doc 03 §8) */
                "Idempotency-Key"?: components["parameters"]["IdempotencyKey"];
            };
            path: {
                id: components["parameters"]["PathId"];
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description restored */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["Task"];
                };
            };
            default: components["responses"]["Error"];
        };
    };
    purgeTaskArtifacts: {
        parameters: {
            query?: never;
            header?: {
                /** @description the same key within 24 hours returns the first response (doc 03 §8) */
                "Idempotency-Key"?: components["parameters"]["IdempotencyKey"];
            };
            path: {
                id: components["parameters"]["PathId"];
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": {
                    /** @description tos://.../<run_id>/ exactly as the server computes it */
                    confirm_path: string;
                };
            };
        };
        responses: {
            /** @description purge started */
            202: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": {
                        path: string;
                        bytes: number;
                        latest_removed?: boolean;
                    };
                };
            };
            default: components["responses"]["Error"];
        };
    };
    rebindTaskCredentials: {
        parameters: {
            query?: never;
            header?: {
                /** @description the same key within 24 hours returns the first response (doc 03 §8) */
                "Idempotency-Key"?: components["parameters"]["IdempotencyKey"];
            };
            path: {
                id: components["parameters"]["PathId"];
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": {
                    input_credential?: string;
                    output_credential?: string;
                };
            };
        };
        responses: {
            /** @description rebound */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["Task"];
                };
            };
            default: components["responses"]["Error"];
        };
    };
    taskAction: {
        parameters: {
            query?: never;
            header?: {
                /** @description the same key within 24 hours returns the first response (doc 03 §8) */
                "Idempotency-Key"?: components["parameters"]["IdempotencyKey"];
            };
            path: {
                id: components["parameters"]["PathId"];
                action: "start" | "pause" | "resume" | "stop";
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description new state */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["Task"];
                };
            };
            default: components["responses"]["Error"];
        };
    };
    repreflightTask: {
        parameters: {
            query?: never;
            header?: {
                /** @description the same key within 24 hours returns the first response (doc 03 §8) */
                "Idempotency-Key"?: components["parameters"]["IdempotencyKey"];
            };
            path: {
                id: components["parameters"]["PathId"];
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description result */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["RepreflightResult"];
                };
            };
            default: components["responses"]["Error"];
        };
    };
    retryTask: {
        parameters: {
            query?: never;
            header?: {
                /** @description the same key within 24 hours returns the first response (doc 03 §8) */
                "Idempotency-Key"?: components["parameters"]["IdempotencyKey"];
            };
            path: {
                id: components["parameters"]["PathId"];
            };
            cookie?: never;
        };
        requestBody?: {
            content: {
                "application/json": {
                    /** @description omitted = every module with error episodes or a whole-module failure */
                    modules?: components["schemas"]["ModuleId"][];
                };
            };
        };
        responses: {
            /** @description subtask created */
            202: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["SubtaskCreated"];
                };
            };
            default: components["responses"]["Error"];
        };
    };
    continueTask: {
        parameters: {
            query?: never;
            header?: {
                /** @description the same key within 24 hours returns the first response (doc 03 §8) */
                "Idempotency-Key"?: components["parameters"]["IdempotencyKey"];
            };
            path: {
                id: components["parameters"]["PathId"];
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description subtask created */
            202: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["SubtaskCreated"];
                };
            };
            default: components["responses"]["Error"];
        };
    };
    reexportTask: {
        parameters: {
            query?: never;
            header?: {
                /** @description the same key within 24 hours returns the first response (doc 03 §8) */
                "Idempotency-Key"?: components["parameters"]["IdempotencyKey"];
            };
            path: {
                id: components["parameters"]["PathId"];
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description subtask created */
            202: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["SubtaskCreated"];
                };
            };
            default: components["responses"]["Error"];
        };
    };
    listSubtasks: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                id: components["parameters"]["PathId"];
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description subtasks, oldest first */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": {
                        items: components["schemas"]["Subtask"][];
                    };
                };
            };
            default: components["responses"]["Error"];
        };
    };
    getTaskTimeline: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                id: components["parameters"]["PathId"];
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description timeline, oldest first */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": {
                        items: components["schemas"]["TimelineEntry"][];
                    };
                };
            };
            default: components["responses"]["Error"];
        };
    };
    getTaskPlan: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                id: components["parameters"]["PathId"];
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description plan */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["plan.schema"];
                };
            };
            default: components["responses"]["Error"];
        };
    };
    getTaskLogs: {
        parameters: {
            query?: {
                stage?: string;
                /** @description absent = everything, empty = main run only, an id = that subtask only */
                subtask?: string;
                level?: "error" | "warn" | "info" | "debug";
                /** @description opaque, from next_cursor */
                cursor?: components["parameters"]["Cursor"];
                limit?: components["parameters"]["Limit"];
            };
            header?: never;
            path: {
                id: components["parameters"]["PathId"];
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description log lines */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["CursorPage"] & {
                        items?: components["schemas"]["LogLine"][];
                    };
                };
            };
            default: components["responses"]["Error"];
        };
    };
    getTaskUsage: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                id: components["parameters"]["PathId"];
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description usage */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["UsageReport"];
                };
            };
            default: components["responses"]["Error"];
        };
    };
    getReport: {
        parameters: {
            query?: {
                /** @description result revision; omitted = the current one (task.result_rev) */
                rev?: components["parameters"]["Rev"];
            };
            header?: never;
            path: {
                id: components["parameters"]["PathId"];
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description report */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": {
                        revision: number;
                        report: components["schemas"]["report.schema"];
                        links: components["schemas"]["Links"];
                    };
                };
            };
            default: components["responses"]["Error"];
        };
    };
    getReportTable: {
        parameters: {
            query?: {
                /** @description result revision; omitted = the current one (task.result_rev) */
                rev?: components["parameters"]["Rev"];
                /** @description opaque, from next_cursor */
                cursor?: components["parameters"]["Cursor"];
                limit?: number;
                /** @description a column from the table's sortable whitelist */
                sort?: string;
                order?: "asc" | "desc";
            };
            header?: never;
            path: {
                id: components["parameters"]["PathId"];
                /** @description a table id declared in the module registry */
                table: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description rows */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["CursorPage"] & {
                        columns: string[];
                        revision: number;
                        items?: Record<string, unknown>[];
                    };
                };
            };
            default: components["responses"]["Error"];
        };
    };
    listTaskEpisodes: {
        parameters: {
            query?: {
                /** @description result revision; omitted = the current one (task.result_rev) */
                rev?: components["parameters"]["Rev"];
                list?: "passed" | "reject" | "held";
                /** @description only episodes with (true) or without (false) an open question */
                review?: boolean;
                /** @description only episodes with a finding of this level (findings revisions, 2.3.0) */
                level?: components["schemas"]["FindingLevel"];
                /** @description only episodes with a finding of this taxonomy item (findings revisions, 2.3.0); not an item id is 400 */
                item?: string;
                /** @description an episode number, optionally with the ep prefix: 12, ep12, ep 12 */
                q?: string;
                /** @description opaque, from next_cursor */
                cursor?: components["parameters"]["Cursor"];
                limit?: number;
            };
            header?: never;
            path: {
                id: components["parameters"]["PathId"];
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description episodes */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["TaskEpisodePage"];
                };
            };
            default: components["responses"]["Error"];
        };
    };
    getEpisode: {
        parameters: {
            query?: {
                /** @description result revision; omitted = the current one (task.result_rev) */
                rev?: components["parameters"]["Rev"];
            };
            header?: never;
            path: {
                id: components["parameters"]["PathId"];
                index: number;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description episode view */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["EpisodeView"];
                };
            };
            default: components["responses"]["Error"];
        };
    };
    listPipelineEpisodes: {
        parameters: {
            query?: {
                /** @description Change sequence cursor returned as next_cursor; shows the most recently updated episodes first. */
                before?: number;
                limit?: number;
            };
            header?: never;
            path: {
                id: components["parameters"]["PathId"];
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Recent episodes and live funnel verdicts */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": {
                        items: components["schemas"]["PipelineEpisode"][];
                        next_cursor: number | null;
                        started: number;
                        finished: number;
                        /** @description two-block runs (2.3.0): per per-episode module so far, the episodes it judged, the ones it failed on and, of the judged, the ones with a finding (the live module cards) */
                        modules?: {
                            id: components["schemas"]["ModuleId"];
                            judged: number;
                            error: number;
                            flagged: number;
                        }[];
                    };
                };
            };
            default: components["responses"]["Error"];
        };
    };
    getPipelineEpisode: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                id: components["parameters"]["PathId"];
                index: number;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Episode progress and current records */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["PipelineEpisode"];
                };
            };
            default: components["responses"]["Error"];
        };
    };
    getEpisodeSyncCurves: {
        parameters: {
            query?: {
                /** @description result revision; omitted = the current one (task.result_rev) */
                rev?: components["parameters"]["Rev"];
            };
            header?: never;
            path: {
                id: components["parameters"]["PathId"];
                index: number;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description curves */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["SyncCurves"];
                };
            };
            default: components["responses"]["Error"];
        };
    };
    getTaskViz: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                id: components["parameters"]["PathId"];
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description the task's input */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["VizDataset"];
                };
            };
            default: components["responses"]["Error"];
        };
    };
    getTaskEpisodeViz: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                id: components["parameters"]["PathId"];
                /** @description the episode's own index (preflight `episode_indices`) */
                index: components["parameters"]["PathIndex"];
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description the episode */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["VizEpisode"];
                };
            };
            default: components["responses"]["Error"];
        };
    };
    getTaskEpisodeSeries: {
        parameters: {
            query: {
                /** @description the curve group's `key` (VizStream.key) */
                stream: components["parameters"]["VizStream"];
                /** @description start of the window in episode seconds (default 0) */
                from?: components["parameters"]["VizFrom"];
                /** @description end of the window in episode seconds (default the episode's end) */
                to?: components["parameters"]["VizTo"];
                /** @description at most this many points per line; more raw points are thinned by min / max per bucket */
                points?: components["parameters"]["VizPoints"];
            };
            header?: never;
            path: {
                id: components["parameters"]["PathId"];
                /** @description the episode's own index (preflight `episode_indices`) */
                index: components["parameters"]["PathIndex"];
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description the curves */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["VizSeries"];
                };
            };
            default: components["responses"]["Error"];
        };
    };
    getTaskCameraVideo: {
        parameters: {
            query?: {
                /** @description true: the H.264 transcode of the camera, for a browser that cannot decode the original (AV1 without hardware decoding, HEVC); 404 with details.reason transcode_disabled when CURATOR_VIZ_TRANSCODE is off */
                transcode?: components["parameters"]["VizTranscode"];
            };
            header?: {
                /** @description one byte range (`bytes=a-b`, `bytes=a-`, `bytes=-n`) */
                Range?: components["parameters"]["RangeHeader"];
            };
            path: {
                id: components["parameters"]["PathId"];
                /** @description the episode's own index (preflight `episode_indices`) */
                index: components["parameters"]["PathIndex"];
                /** @description the camera's `key` in the presentation model */
                camera: components["parameters"]["PathCamera"];
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            200: components["responses"]["VizVideo"];
            202: components["responses"]["VizPending"];
            206: components["responses"]["VizVideoRange"];
            /** @description the range is outside the file */
            416: {
                headers: {
                    [name: string]: unknown;
                };
                content?: never;
            };
            default: components["responses"]["Error"];
        };
    };
    getTaskCameraFrames: {
        parameters: {
            query?: never;
            header?: {
                /** @description one byte range (`bytes=a-b`, `bytes=a-`, `bytes=-n`) */
                Range?: components["parameters"]["RangeHeader"];
            };
            path: {
                id: components["parameters"]["PathId"];
                /** @description the episode's own index (preflight `episode_indices`) */
                index: components["parameters"]["PathIndex"];
                /** @description the camera's `key` in the presentation model */
                camera: components["parameters"]["PathCamera"];
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            200: components["responses"]["VizFrames"];
            202: components["responses"]["VizPending"];
            206: components["responses"]["VizFramesRange"];
            /** @description the range is outside the pack */
            416: {
                headers: {
                    [name: string]: unknown;
                };
                content?: never;
            };
            default: components["responses"]["Error"];
        };
    };
    getTaskCameraFrameIndex: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                id: components["parameters"]["PathId"];
                /** @description the episode's own index (preflight `episode_indices`) */
                index: components["parameters"]["PathIndex"];
                /** @description the camera's `key` in the presentation model */
                camera: components["parameters"]["PathCamera"];
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description the index */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["VizFrameIndex"];
                };
            };
            202: components["responses"]["VizPending"];
            default: components["responses"]["Error"];
        };
    };
    getPerf: {
        parameters: {
            query?: {
                /** @description result revision; omitted = the current one (task.result_rev) */
                rev?: components["parameters"]["Rev"];
                scope?: "all" | "main" | "subtask";
                subtask?: string;
            };
            header?: never;
            path: {
                id: components["parameters"]["PathId"];
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description profile */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["Perf"];
                };
            };
            default: components["responses"]["Error"];
        };
    };
    listAdjudication: {
        parameters: {
            query?: {
                source?: components["schemas"]["ModuleId"];
                /** @description by card status, the same on both tabs: pending = not answered yet (on the appeals tab too, although appeals never count in AdjudicationCounts.pending) or answered unsure; decided; unapplied = answered but not executed yet */
                status?: "pending" | "decided" | "unapplied" | "all";
                /** @description appeals lists rejects attributed to an appealable module (registry `appealable`; today task_success alone and dedup); physical and structural gates and soft scores are final */
                tab?: "review" | "appeals";
                /** @description opaque, from next_cursor */
                cursor?: components["parameters"]["Cursor"];
                limit?: components["parameters"]["Limit"];
            };
            header?: never;
            path: {
                id: components["parameters"]["PathId"];
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description cards */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["CursorPage"] & {
                        items?: components["schemas"]["AdjudicationCard"][];
                        counts: components["schemas"]["AdjudicationCounts"];
                    };
                };
            };
            default: components["responses"]["Error"];
        };
    };
    submitAdjudication: {
        parameters: {
            query?: never;
            header?: {
                /** @description the same key within 24 hours returns the first response (doc 03 §8) */
                "Idempotency-Key"?: components["parameters"]["IdempotencyKey"];
            };
            path: {
                id: components["parameters"]["PathId"];
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": {
                    decisions: components["schemas"]["DecisionInput"][];
                };
            };
        };
        responses: {
            /** @description recorded */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AdjudicationCounts"];
                };
            };
            default: components["responses"]["Error"];
        };
    };
    applyAdjudication: {
        parameters: {
            query?: never;
            header?: {
                /** @description the same key within 24 hours returns the first response (doc 03 §8) */
                "Idempotency-Key"?: components["parameters"]["IdempotencyKey"];
            };
            path: {
                id: components["parameters"]["PathId"];
            };
            cookie?: never;
        };
        requestBody?: {
            content: {
                "application/json": components["schemas"]["AdjudicationApply"];
            };
        };
        responses: {
            /** @description subtask created */
            202: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["SubtaskCreated"];
                };
            };
            default: components["responses"]["Error"];
        };
    };
    signMedia: {
        parameters: {
            query: {
                task: string;
                /** @description delivery (output key) or input (input key; the public cache bucket is not signed) */
                scope: "delivery" | "input";
                /** @description relative to the scope's prefix - scope=delivery: the task's run directory <delivery>/<run_id>/; scope=input: the dataset root - normalized and checked against it. Clip start and end times come from the episode endpoint. */
                path: string;
                ttl?: number;
            };
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description signed URL (public endpoint) */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["SignedUrl"];
                };
            };
            default: components["responses"]["Error"];
        };
    };
    taskEvents: {
        parameters: {
            query?: never;
            header?: {
                "Last-Event-ID"?: string;
            };
            path: {
                id: components["parameters"]["PathId"];
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description event stream */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "text/event-stream": string;
                };
            };
            default: components["responses"]["Error"];
        };
    };
    healthz: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description alive */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": {
                        /** @constant */
                        status: "ok";
                    };
                };
            };
        };
    };
    readyz: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description ready */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["Readiness"];
                };
            };
            /** @description not ready */
            503: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["Readiness"];
                };
            };
        };
    };
}
