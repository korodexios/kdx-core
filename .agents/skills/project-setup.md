# Skill: project-setup

Keep korodexios-core a dependable foundation.

## When to use
Touching `pyproject.toml`, packaging, module discovery, or the runtime layout.

## Steps
1. The version lives in `src/korodexios_core/version.py` only; `pyproject.toml` reads it
   dynamically (`dynamic = ["version"]`). Never duplicate it.
2. New package data goes in `[tool.hatch.build.targets.wheel].artifacts` and must be
   verified inside the built wheel, not just on disk.
3. Built-in modules live in `src/korodexios_core/modules/<name>/` with a
   `korodex_module.toml` manifest; that directory stays a pure discovery root.
4. Runtime state belongs under `var/` (`log/`, `lib/`, `chassis.ipc`): out of git,
   out of the source tree.
5. Build and audit before releasing: `uv build`, then inspect wheel and sdist.

## Verification
`make check` plus a wheel audit: `py.typed` and every `korodex_module.toml` present.
