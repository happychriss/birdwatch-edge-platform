---
name: project-setup
description: Working conventions for this project — folder structure, knowledge flow, and development workflow
---

# Project Setup

## 0. Environment Setup

Run once to ensure `~/.local/bin` (where tools like `claude` install) is on the PATH:

```bash
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.zshrc && source ~/.zshrc
```

This is required when `claude install` warns that the install location is not in PATH.

---

## 1. Folder Structure

Ensure these folders exist at the workspace root:

```
/workspace/
├── requirements.md  # ROOT — project type pointer + functional specification for all features
├── external-docs/   # Raw external material — reference docs, API specs, vendor docs (read-only)
├── knowledge/        # Curated learned notes — confirmed working config derived from docs + experiment
├── skills/          # Reusable Claude skills (this file lives here)
└── src/             # Source projects
```

Create any missing folders. Do not create a README in `docs/` or `knowledge/` — the files are self-describing.

---

## 2. Knowledge Flow

This is the rule for where project knowledge lives:

```
external-docs/    →    knowledge/           →    memory.md
(reference material)   (confirmed config)       (thin pointers only)
```

### external-docs/
- Drop zone for external material: API docs, reference markdown, vendor docs
- Never edit these files
- When the user adds a file here, analyse it and create or update the corresponding `knowledge/` file

### knowledge/
- This is the single source of truth — not memory.md
- Knowledge learned from working on this project, mistakes made and how they were fixed

### memory.md
- Stays concise: pointers to `knowledge/` files + project-level gotchas
- Never duplicate detail that belongs in `knowledge/`

---

## 3. Analysing a New Doc

When the user adds a file to `external-docs/`:

1. Read it fully
2. Extract: key concepts, configuration, API details, any quirks or caveats
3. Create `knowledge/<topic>.md` with structured notes
4. Mark unconfirmed values clearly: `# unconfirmed — from docs`
5. Tell the user what was captured and what needs validation

---

## 4. Write-Back Rule

At the end of any session where behaviour was confirmed experimentally:
1. Update the relevant `knowledge/` file with the confirmed values
2. Trim any duplicate detail from `memory.md` — keep only the pointer and project-level gotchas
