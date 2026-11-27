# Needletail integration 6 on main

Integration 6 is the latest published build with a four-region Linode run and a referee record. The merge starts from the corrected Wave 12 record at `edb966f`, which contains the tested Needletail deployment at `6e40240` and the Wave 12 scoreboards. It also includes two pre-merge corrections. The small 4K topology explicitly declares its existing 247 Mbit/s planning rate, and the compiler rejects a standby receive socket that collides with a downstream sender socket on the same forwarder.

| Component | Revision used in cmpd9 |
| --- | --- |
| Needletail deployment | `6e402408dd510916bd6813015c638ed526032724` |
| Compare harness | `28d769864f837f51680b1ed59cb2e11e525ce7de` |
| av-mesh | `74d0c042027a35e40486f8c41103d9f82387c479` |
| av-contrib | `8a6a8bc0e50323e2b81a0a64d28e228b9480dd76` |
| playlists | `68f235be997e34b06d4cf1b97a33e1742022cb9c` |

The test ran on dedicated Linode instances in London, New York, Tokyo, and Sydney. The versions above identify the historical test. The comparison and review commits that follow it do not produce another network measurement. [Wave 12 run, settings, and results](2026-11-22-4k-against-cloudflare-and-livekit.md#latest-integration-6-against-livekit-26-november-night)

## What the run measured

- **Audio.** Needletail won 58 of 64 rows against self-hosted LiveKit, with one tie and five losses. Against LiveKit Cloud it won 63 of 64. Excluding rows marked with a measurement host stop gives 46 wins, one tie, and four losses against self-hosted, and 51 wins against LiveKit Cloud. The 1 ms Opus Custom lane lost zero units across all four regions at 1%, 5%, and burst loss. [Self-hosted results](evidence/20261126-4k-wave12-audio-scoreboard.md), [Cloud results](evidence/20261126-4k-wave12-audio-scoreboard-lkcloud.md)
- **4K.** The 300-second 247 Mbit/s soak met the better-viewer rule in all four regions, with late or refused second viewers in London and Sydney. The same rate missed the London ladder gate at 99.44% on time against the 99.5% threshold. Stagger and repair passed in London only. The first held session failed the rule in every region. The recorder left sessions two and three unscored. [Soak](evidence/20261126-4k-wave12-video-w12-soak-scoreboard.md), [ladder](evidence/20261126-4k-wave12-video-w12-scoreboard.md), [held-session correction](2026-11-22-4k-against-cloudflare-and-livekit.md#latest-integration-6-against-livekit-26-november-night)
- **PCM beside 4K.** The UDP PCM probes lost zero parts across all 32 rows. The LL-HLS probes that played lost at most 0.0036%, with two Tokyo HTTP 429s. There were no OOM kills. Forwarder RSS grew during the soak. [Video and audio evidence](evidence/20261126-4k-wave12-video-audio.md)
- **Audience size.** The London load generator saturated at 120 listeners before the first missing part. The crowd test did not establish the edge or global audience ceiling. [Crowd summary](evidence/20261126-4k-wave12-crowd-summary.json)

## Integration 7 handoff

The integration 7 work is in the local `rm/int8` workspace and its follow-on comparison tooling. The block 4 rerun recorded clean single-chain failover with no missing parts and passed the local audio gate. Later admission and chain-aware placement work was in progress. Its combined validation still needed the block 5 campaign, including reader egress, repeated dips with return to the planned primary, forced-move catch-up, cross-build, and the independent network run.

The `cmpd9` manifest marks collection complete and records teardown at 02:51 UTC on 26 November. Its billed cost was $4.212. The merge checks ran on the existing Linode dev box. [Archive references and verification](evidence/20261127-integration-6-main.json)
