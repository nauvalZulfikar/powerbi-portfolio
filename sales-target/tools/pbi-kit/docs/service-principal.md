# Service principal setup (one-time, needs Entra + Power BI/Fabric admin)

1. **Entra ID → App registrations → New**: name `pbi-kit-ci`. Note tenant id + client id.
2. **Credential** — pick one:
   - GitHub OIDC (preferred, no secret): Certificates & secrets → Federated credentials → GitHub Actions →
     repo `<owner>/<repo>`, entity type **Environment**, names `Test` and `Prod` (one credential each).
   - Client secret: create one, store as `AZURE_CLIENT_SECRET` environment secret. Rotate before expiry.
3. **Security group**: create `sg-pbi-kit-ci`, add the app.
4. **Admin portal → Tenant settings** (enable for that group only):
   - "Service principals can use Fabric APIs" (older name: "Allow service principals to use Power BI APIs")
   - "Dataset Execute Queries REST API" (for online DAX tests)
   - "Users can create Fabric items" if deploying Fabric items (Lakehouse etc.)
5. **Workspaces**: add the app as **Contributor** (publish) or **Admin** (if it must manage permissions) on Test and Prod.
6. **Gateway / cloud connections**: add the SP as user of each connection the models bind to, otherwise refresh fails after publish.
7. GitHub: environment secrets `AZURE_CLIENT_ID`, `AZURE_TENANT_ID` (+ secret if not OIDC); variables `FABRIC_WS_TEST`, `FABRIC_WS_PROD`, `PBI_DATASET_NAME`.

## Known limits
- **executeQueries does not accept a service principal on a model with RLS** (Microsoft limitation). For
  those models the online DAX test step needs a user token (`PBI_ACCESS_TOKEN`) or is skipped; RLS itself
  is tested in Desktop "View as" (evidence screenshots).
- A SP cannot own a model that uses SSO data sources; publish works but refresh credentials must be set by a user once.
- Pro workspace (no capacity): fabric-cicd publish works for SemanticModel/Report; Fabric items need capacity.
