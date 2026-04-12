# Changelog

## 1.0.0 (2026-04-12)


### Features

* swap PM2/systemd for Docker ([70973c7](https://github.com/ghndrx/hearth-agents/commit/70973c70ec534e455681eb39a98bf2a65c8ab82e))


### Bug Fixes

* concurrency limiter prevents API flooding ([04768c0](https://github.com/ghndrx/hearth-agents/commit/04768c0590f857363a6372dc073e97c9f7bb93d7))
* proper finally block for concurrency limiter release ([12a6366](https://github.com/ghndrx/hearth-agents/commit/12a63660a6ff7ff61db34d48011665dafe2c02e2))
* skip idea engine on startup, reduce wikidelve timeout to 3s ([a9bed63](https://github.com/ghndrx/hearth-agents/commit/a9bed63ba1ad77a462d5001571b1028c0758a648))


### Performance Improvements

* drastically cut research phase to reach implementation faster ([fe2b4c1](https://github.com/ghndrx/hearth-agents/commit/fe2b4c197124af3baa50d8537fc4df97a1d48c16))
