# Third-party notices

microlms-core is licensed under the GNU Affero General Public License v3.0 or later (see `LICENSE`). It includes the third-party components below, each under its own license.

## Shipped in the package

| Component | Location | License | Copyright |
| --- | --- | --- | --- |
| [scorm-again](https://github.com/jcputney/scorm-again) 3.5.0 | `src/microlms/modules/scorm/scorm-again-*.min.js` | MIT (`scorm-again.LICENSE.txt`) | Jonathan Putney |
| [h5p-standalone](https://github.com/tunapanda/h5p-standalone) player | `src/microlms/modules/h5p/assets/` | MIT (`assets/LICENSE.txt`) | Tunapanda |
| H5P core runtime, styles and icon fonts, bundled by h5p-standalone ([h5p-php-library](https://github.com/h5p/h5p-php-library)) | `src/microlms/modules/h5p/assets/frame.bundle.js`, `styles/`, `fonts/h5p-*` | GPL-3.0 | Joubel |
| [Inter](https://github.com/rsms/inter) font | `src/microlms/modules/h5p/assets/fonts/inter/` | SIL OFL 1.1 (`LICENSE.txt`) | The Inter Project Authors |
| [Open Sans](https://github.com/googlefonts/opensans) font | `src/microlms/modules/h5p/assets/fonts/open-sans/` | SIL OFL 1.1 (`OFL.txt`) | The Open Sans Project Authors |

## Test fixtures (repository only, not in the package)

| Fixture | Source | License |
| --- | --- | --- |
| `tests/fixtures/golf_scorm12.zip`, `golf_scorm2004.zip` | Rustici Software's [Golf Explained](https://scorm.com/scorm-explained/technical-scorm/golf-examples/) examples | CC BY 3.0 US, by Rustici Software, LLC; page text attributed in-package to Wikipedia and WikiHow |
| `tests/fixtures/geology_cmi5.zip` | [ADL CATAPULT](https://github.com/adlnet/CATAPULT) course examples | Apache-2.0; NOTICE: "Catapult. Copyright 2020 Rustici Software. This product includes software developed at Rustici Software (http://www.rusticisoftware.com/)." |
| `tests/fixtures/truefalse_h5p.h5p` | h5p-standalone test package | MIT |
| `tests/fixtures/dragtext_h5p.h5p` | [Lumi H5P-Nodejs-library](https://github.com/Lumieducation/H5P-Nodejs-library) example packages | GPL-3.0 |
