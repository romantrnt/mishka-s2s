# Weather configuration and supported REST contracts

Weather requires all three values: `YANDEX_WEATHER_API_KEY`,
`YANDEX_WEATHER_LATITUDE`, `YANDEX_WEATHER_LONGITUDE`. A key alone does not
identify the installation's location. MISHKA never derives location from a
child profile, an IP address or a guessed city. Missing configuration returns
the existing unavailable response; it does not create synthetic current weather.

## Which file actually wins

`scripts/run_stand.py` loads the explicitly selected `MISHKA_ENV_FILE`, or the
project `.env` when no file is selected. An explicit `.env.physical` does not
also load a nearby `.env`. Initial process environment takes precedence through
`setdefault`, including variables inherited with an empty value. Editing a file
does not change an already running process. Check presence and configuration
source, never publish a key or a complete environment dump. Legacy `WEATHER_*`
aliases remain supported; prefer the canonical `YANDEX_WEATHER_*` names.

The read-only audit on 2026-11-24 found the active garage's explicit
`.env.physical` had a key but neither coordinate; the preserved `.env` copies
had the same situation. A separate request from the developer computer with
Roman's explicit central Saint Petersburg point returned HTTP 200 from the
configured `/v2/forecast`; its actual current `fact` passed the adapter. No
running stand configuration was changed. This establishes that the key/endpoint
worked for that request, not that the running stand has weather configured or
that a particular paid plan has been identified.

For the next separately authorized activation, Roman selected this point:

```dotenv
YANDEX_WEATHER_LATITUDE=59.9343
YANDEX_WEATHER_LONGITUDE=30.3351
```

These are explicit installation settings, not defaults or a hardcoded city.
During a demonstration, do not apply them, restart or send test events to the
live stand. Recheck the next process's effective configuration after activation.

## Endpoint contract

[Yandex's REST documentation](https://yandex.ru/dev/weather/doc/ru/concepts/errors)
explains that endpoint access depends on the plan and that HTTP 403 has several
possible causes, including a missing key, wrong endpoint, expiry and quota.
Do not infer the exact cause from 403 alone. Choose the endpoint explicitly;
MISHKA does not retry a denied key against another endpoint or a demo provider.

- `/v2/forecast` retains `lat`, `lon`, `lang`, `limit=1`, `hours=false`.
- `/v2/informers` sends only its documented `lat`, `lon`, `lang` parameters.
  Its prediction object is singular `forecast`, unlike `forecasts` in the
  forecast response. See the [informers contract](https://yandex.ru/dev/weather/doc/ru/concepts/forecast-info).

Current weather uses validated `fact.temp`, `fact.condition`, `fact.obs_time`
and the expected `info` object. Unused prediction fields do not substitute for
an observation or gate an otherwise valid current fact. Measurement age stays
limited to two hours with five minutes of future clock tolerance. Unknown
conditions, invalid temperatures, missing facts, malformed responses, excessive
responses, network errors and timeouts remain unavailable. Documented rain
conditions including drizzle, moderate and continuous heavy rain are supported.
No redirects, automatic endpoint fallback, synthetic weather or private profile
fields are introduced.

## City and verification limits

The supported REST `info` contract supplies coordinates and a weather-page URL,
not a guaranteed display name. This adapter does not invent a city from a URL
slug. The existing spoken city limitation is retained. A separate configured
name/geocoder contract would need explicit provenance before promising a city.

`tests/test_yandex_weather_endpoints.py` exercises both contracts and denials
using synthetic HTTP; the other weather tests exercise typed menu/dialogue
routing, scope, guest privacy, expiry and failures. Synthetic tests are distinct
from the one isolated external API diagnostic. Neither establishes physical
speaker output or a newly configured live stand.
