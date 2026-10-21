# d3 modules (vendored)

Only the d3 modules the wiki graph needs, as their official minified UMD builds (`dist/<name>.min.js`
from each npm package), unchanged. `d3.js` imports them in dependency order; each adds itself to
`globalThis.d3`. Licence: ISC (each module's `LICENSE-<name>`; d3-ease also carries Robert Penner's
BSD licence for the easing equations).

Downloaded on 2026-09-28 from the npm registry. Each tarball was checked against the registry's
`dist.integrity` (sha512) before extracting. To update: download the new tarballs, check their
integrity, copy `package/dist/<name>.min.js` and `package/LICENSE`, and update this table.

| Module | Version | Tarball (registry.npmjs.org) | Tarball integrity (npm) | sha256 of the .min.js |
| --- | --- | --- | --- | --- |
| d3-dispatch | 3.0.1 | `/d3-dispatch/-/d3-dispatch-3.0.1.tgz` | `sha512-rzUyPU/S7rwUflMyLc1ETDeBj0NRuHKKAcvukozwhshr6g6c5d8zh4c2gQjY2bZ0dXeGLWc1PF174P2tVvKhfg==` | `94b3bbdb6b98dc1325a15762b051013e8253999b0e0436b27d1da17b952ba0af` |
| d3-timer | 3.0.1 | `/d3-timer/-/d3-timer-3.0.1.tgz` | `sha512-ndfJ/JxxMd3nw31uyKoY2naivF+r29V+Lc0svZxe1JvvIRmi8hUsrMvdOwgS1o6uBHmiz91geQ0ylPP0aj1VUA==` | `911ceda305f014b6b53ca68d5c896a9a387da120cfd56a421a2c60cca2fc9b36` |
| d3-quadtree | 3.0.1 | `/d3-quadtree/-/d3-quadtree-3.0.1.tgz` | `sha512-04xDrxQTDTCFwP5H6hRhsRcb9xxv2RzkcsygFzmkSIOJy3PeRJP7sNk3VRIbKXcog561P9oU0/rVH6vDROAgUw==` | `57e2ad12824ed82893ba447523f2a2fb9beeb9222aafb2c778a9f5b313348b0e` |
| d3-force | 3.0.0 | `/d3-force/-/d3-force-3.0.0.tgz` | `sha512-zxV/SsA+U4yte8051P4ECydjD/S+qeYtnaIyAs9tgHCqfguma/aAQDjo85A9Z6EKhBirHRJHXIgJUlffT4wdLg==` | `1e07b473241328795d5ea9ad479a7bbabd765012fa2ef95633c83b69868dff6b` |
| d3-selection | 3.0.0 | `/d3-selection/-/d3-selection-3.0.0.tgz` | `sha512-fmTRWbNMmsmWq6xJV8D19U/gw/bwrHfNXxrIN+HfZgnzqTHp9jOmKMhsTUjXOJnZOdZY9Q28y4yebKzqDKlxlQ==` | `45daab9cf677901bcae102f3f23ca2930db3c0fb8ff9e3dbed087d9c4de921ca` |
| d3-color | 3.1.0 | `/d3-color/-/d3-color-3.1.0.tgz` | `sha512-zg/chbXyeBtMQ1LbD/WSoW2DpC3I0mpmPdW+ynRTj/x2DAWYrIY7qeZIHidozwV24m4iavr15lNwIwLxRmOxhA==` | `a12639010163230b8c130fbeb92a3a49bb5f6989a566d3664d759522db458489` |
| d3-interpolate | 3.0.1 | `/d3-interpolate/-/d3-interpolate-3.0.1.tgz` | `sha512-3bYs1rOD33uo8aqJfKP3JWPAibgw8Zm2+L9vBKEHJ2Rg+viTR7o5Mmv5mZcieN+FRYaAOWX5SJATX6k1PWz72g==` | `bfc321e4c3f3b3aadc88cfe15ccb5e443abfeadef8b75c65b41c33a4d78a98ae` |
| d3-ease | 3.0.1 | `/d3-ease/-/d3-ease-3.0.1.tgz` | `sha512-wR/XK3D3XcLIZwpbvQwQ5fK+8Ykds1ip7A2Txe0yxncXSdq1L9skcG7blcedkOX+ZcgxGAmLX1FrRGbADwzi0w==` | `e60a1ed750a1ad138dd18e8d3f463238113cfbf7d89685a13d19bd4e048dc3ce` |
| d3-transition | 3.0.1 | `/d3-transition/-/d3-transition-3.0.1.tgz` | `sha512-ApKvfjsSR6tg06xrL434C0WydLr7JewBB3V+/39RMHsaXTOG0zmt/OAXeng5M5LBm0ojmxJrpomQVZ1aPvBL4w==` | `c248d97f6c131af7f4a12a3ff1f2ed83f1cc6c0e17fc764a198b6ee452e6621b` |
| d3-drag | 3.0.0 | `/d3-drag/-/d3-drag-3.0.0.tgz` | `sha512-pWbUJLdETVA8lQNJecMxoXfH6x+mO2UQo8rSmZ+QqxcbyA3hfeprFgIT//HW2nlHChWeIIMwS2Fq+gEARkhTkg==` | `158499fa8cdeef459e2ab9d01af22babd003c423a212fb24088c0f838864012b` |
| d3-zoom | 3.0.0 | `/d3-zoom/-/d3-zoom-3.0.0.tgz` | `sha512-b8AmV3kfQaqWAuacbPuNbL6vahnOJflOhexLzMMNLga62+/nh0JzvJ0aO/5a5MVgUFGS7Hu1P9P03o3fJkDCyw==` | `4fdce9b830b78225c58e27258296084253c41a5eaf8cad9bd9ccfe3025daf1cf` |
