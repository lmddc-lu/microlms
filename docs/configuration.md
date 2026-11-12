# Configuration

`microlms.Settings` reads `MICROLMS_`-prefixed environment variables or a `.env` file. Subclass
it in your project to add your own fields. Each setting is documented in
[`src/microlms/config.py`](https://github.com/lmddc-lu/microlms/blob/main/src/microlms/config.py).

Only the database URL is required:

```bash
MICROLMS_DATABASE_URL=postgresql+psycopg://lms:lms@localhost/lms
```

The other settings are needed by the features that use them:

| Feature | Variables |
| --- | --- |
| Application | `MICROLMS_APP_NAME`, `MICROLMS_DEBUG`, `MICROLMS_API_PREFIX` |
| Content modules (`scorm`, `cmi5`, `h5p`, `lti`, `xapi`) | `MICROLMS_LRS_URL`, `MICROLMS_LRS_API_KEY`, `MICROLMS_LRS_API_SECRET`, `MICROLMS_LRS_ACTOR_HOME_PAGE` |
| Content launches | `MICROLMS_LAUNCH_MAX_AGE` |
| Self-registration | `MICROLMS_REGISTRATION_ORG_SLUG` |
| xAPI ingestion (`xapi`) | `MICROLMS_XAPI_INGEST_KEYS`, `MICROLMS_XAPI_GUEST_ROLE`, `MICROLMS_XAPI_GUEST_ORG_SLUG` |
| LTI 1.3 platform (`lti`) | `MICROLMS_LTI_ISSUER`, `MICROLMS_LTI_PRIVATE_KEY` |
