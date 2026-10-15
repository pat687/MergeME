# Moving from SharePoint to eXo Platform

Why we're moving: [ADR 0001](adr/0001-exo-platform-instead-of-sharepoint.md). In short: no
per-seat licence, privileged files stay on our own hardware, and no lock-in.

## 1. Concept map

| In SharePoint / Microsoft 365 | In eXo Platform | Notes |
|---|---|---|
| Team site | **Space** | Members, managers, and the space's own documents, notes, tasks and activity stream |
| Site owners / members / visitors | Space **managers** / **members**, plus platform groups | Permissions are simpler; rebuild them by hand (see §5) |
| Document library | Space **Documents** (each person also has personal Documents) | Folder upload in the browser |
| Office Online co-editing | **ONLYOFFICE** (included in `make up-full`) | Opens .docx/.xlsx/.pptx in the browser |
| Site pages / wiki | **Notes** | |
| Planner / task lists | **Tasks** (per space) | |
| Custom lists (e.g. a lead tracker) | The **Lead Router** (optionally mirrored into eXo's lead-capture add-on) | Business data belongs in a real database |
| Search | Unified search (Elasticsearch, included) | Indexing runs in the background after upload |
| Teams chat | eXo 7 chat runs on **Matrix**, which needs its own Matrix server | Not included in this stack. eXo logs `Could not initialize Matrix service` without one. |

## 2. Suggested space layout

| Space | Who | What lives there |
|---|---|---|
| **Intake Operations** | Ops staff | Scripts, QA checklists, escalation procedures (Notes); daily tasks |
| **Attorney Network** | Account managers | Attorney agreements, onboarding packets, territory maps, pricing sheets |
| **Compliance** | Ops lead + counsel | Approved disclosure versions, consent wording history, state-by-state notes, audits |
| **Marketing** | Marketing | Campaign briefs, landing-page copy, creative (edit in ONLYOFFICE) |
| **Management** | Leadership | Financials, board material (restricted membership) |

## 3. Migration plan

| Phase | Steps | Done when |
|---|---|---|
| 1. Inventory | List every site and library, its owner, size and whether it's still used | Spreadsheet signed off by owners |
| 2. Pilot | Move one small library (e.g. Marketing) end to end and have its users work in eXo for a week | Users confirm nothing is missing |
| 3. Bulk copy | Export from SharePoint (§4), import into eXo (§5), verify (§6) | Counts match |
| 4. Permissions | Recreate access per space; restricted material goes in restricted spaces | Owner review done |
| 5. Cutover | Set SharePoint libraries to read-only, then do a final delta copy | Users working in eXo |
| 6. Decommission | Keep SharePoint read-only for 30–90 days, then archive and cancel | Licences cancelled |

## 4. Export from SharePoint

**Small libraries (a few GB):** open the library in the browser, select all, then **Download**
(you get a zip), or use **Sync** to pull it to a PC with OneDrive.

**Bulk, scripted:** [rclone](https://rclone.org/onedrive/) talks to SharePoint document
libraries through its `onedrive` backend.

```bash
rclone config                  # new remote "sp", type "onedrive", then choose the SharePoint
                               # site option and pick the document library
rclone lsd sp:                 # check you can see the library
rclone copy sp: ./export/marketing --progress --create-empty-src-dirs
rclone check sp: ./export/marketing   # compare sizes and hashes
```

## 5. Import into eXo

**Option A: browser upload (simplest, recommended for the pilot).** Open the space, go to
**Documents**, and drag the exported folders onto the page.

**Option B: WebDAV (bulk).** eXo 7.2 exposes its document drives over WebDAV at:

```
http://exoapp.local/webdav/drives/
```

Verified on this stack: the endpoint is live and uses **HTTP Digest** authentication
(realm `webdav-domain`). Not verified: because eXo stores password hashes, Digest login
needs eXo-side configuration. Follow the eXo administrator guide for WebDAV access in your
version, test with a WebDAV client, then copy with rclone:

```bash
rclone config                  # new remote "exo", type "webdav", vendor "other",
                               # url http://exoapp.local/webdav/drives/
rclone copy ./export/marketing "exo:<space drive path>/Marketing" --progress
```

## 6. Verify

- [ ] File counts and total size match the export (`rclone size`, or `find . -type f | wc -l`)
- [ ] Spot-check 10 files per library: they open, and Office files edit in ONLYOFFICE
- [ ] Search finds a known document by a word inside it (allow time for indexing)
- [ ] Permissions: a non-member cannot see a restricted space
- [ ] Owners sign off

## 7. What does NOT move automatically

| Item | What to do |
|---|---|
| Version history | Only the latest version is exported. Archive the SharePoint export if history matters. |
| Custom metadata columns | Export the list view to CSV; keep it with the files or turn columns into folders/tags |
| Permissions | Recreate them per space (§2) |
| Power Automate flows | Rebuild only the ones still needed; lead-related automation lives in the Lead Router |
| Teams chats | Export for records; eXo chat needs a Matrix server (see §1) |
