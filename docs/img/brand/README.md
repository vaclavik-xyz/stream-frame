# Brand

The mark is one continuous stroke around the visor rim: constant weight, round caps, the nose
notch rising in the middle of the bottom edge. Drawn as vector, so the same geometry is the
1024 px icon, the 16 px favicon and the social preview — there is no second, "small" drawing
to keep in sync.

| File | What it is |
| --- | --- |
| `mark.svg` | the mark alone, transparent, 512 grid |
| `icon.svg` | app icon on the dark rounded square |
| `icon-1024.png`, `icon-512.png`, `icon-256.png` | rendered icons |
| `lockup.svg` / `logo.png` | mark + wordmark, horizontal |
| `og.png` | 1200×630 social preview |

Colours: background `#0B1220`, ink `#E8EEF7`, muted `#7C8CA6`, and the ribbon runs
`#4B3BFF` → `#39D0FF` → `#8FA6FF` → `#B98BFF` along a bottom-left to top-right axis.

The player page carries `server/web/favicon.ico`, `icon.svg` and `apple-touch-icon.png`; the
daemon serves those without a token because browsers and iOS request them unprompted.

GitHub's social preview is not settable through the API — upload `og.png` under
*Settings → Social preview*.
