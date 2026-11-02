# Standalone extraction plan
Scope: independent MIT-licensed UPM Editor package, public GitHub repository as requested.
Preserve the game repository and exclude all models, materials, textures, credentials and local evidence.
1. Extract four Editor classes and Blender helpers into a package, with a neutral namespace/menu/output.
2. Resolve the backend from PackageInfo.resolvedPath, not game-relative paths.
3. Port process tests and add package location/safety tests. Run actual Blender regressions using external private fixtures and Unity tests in the isolated validation copy.
4. Audit the exact Git allowlist, commit tool sources/docs only, create a public remote and push if authenticated.
No arbitrary Rigify support, asset licensing changes or cross-platform guarantees.
