# Agent instructions (ExelCode)

## Versioning for pushes/releases
- Bump version on every push depending on significance:
  - **Patch (x.y.Z+1, hundredths)**: bug fixes, small tweaks, minor cleanup, tests/docs-only follow-ups.
  - **Minor (x.Y+1.0, tenths)**: notable features or meaningful behavior changes (non-breaking).
  - **Major (X+1.0.0, whole)**: only for fundamentally new / breaking / "fatally new" changes.
- Update `aifree/__init__.py` (`__version__`) and any user-facing version strings (e.g. README banner) accordingly before committing/pushing.
- Do not forget to apply the bump as part of the pushed work.
