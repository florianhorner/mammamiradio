<!-- Trimmed real capture of https://developers.openai.com/api/docs/deprecations served as text/markdown
     (2026-09-08): front matter, every 'Upcoming deprecations' section, the two 'Past deprecations' sections
     the tests read, and the 2024-08-29 fine-tuning table (a table with a date column that must never stamp
     a model). Everything else on the page was removed. -->
# Deprecations

> For the complete documentation index, see [llms.txt](/llms.txt). Markdown versions of documentation pages are available by appending `.md` to the page URL.

## Overview

As we launch safer and more capable models, we regularly retire older models. Software relying on OpenAI models may need occasional updates to keep working. Impacted customers will always be notified by email and in our documentation along with [blog posts](https://openai.com/blog) for larger changes.

This page lists all API deprecations, along with recommended replacements.

## Model deprecation notice periods

We provide advance notice before retiring models so customers have time to plan and migrate. When we announce a model deprecation, we notify customers who are actively using the model by email and document the deprecation on this page.

Unless safety or compliance concerns require a faster timeline, we provide the following minimum notice periods before model retirement:

- **Generally available models:** At least 6 months.
- **Specialized variants of generally available models:** At least 3 months. Examples include chat variants such as `gpt-5.1-chat-latest`, Codex variants such as `gpt-5.3-codex`, and deep research variants such as `o3-deep-research`.
- **Preview models:** Preview models, identified by `preview` in the model name, may be retired with much shorter notice, such as 2 weeks. Examples include `computer-use-preview` and `gpt-4o-audio-preview`. We don't recommend using preview models for business-critical production workloads unless you can migrate on short notice.

If safety or compliance concerns require us to retire a model sooner, we will provide as much notice as reasonably possible.

These notice periods give customers time to evaluate recommended replacement models, test application behavior, and complete migrations before a model is no longer available. In some cases, developers may be able to provision dedicated capacity for continued access after a model's shutdown date. To explore this option, [contact our sales team](https://openai.com/contact-sales/).

## Deprecation vs. legacy

We use the term "deprecation" to refer to the process of retiring a model or endpoint. When we announce that a model or endpoint is being deprecated, it immediately becomes deprecated. All deprecated models and endpoints will also have a shut down date. At the time of the shut down, the model or endpoint will no longer be accessible.

We use the terms "sunset" and "shut down" interchangeably to mean a model or endpoint is no longer accessible.

We use the term "legacy" to refer to models and endpoints that no longer receive updates. We tag endpoints and models as legacy to signal to developers where we're moving as a platform and that they should likely migrate to newer models or endpoints. You can expect that a legacy model or endpoint will be deprecated at some point in the future.

## Upcoming deprecations

Upcoming deprecations are listed below, with the most recent announcements at the top.

### 2026-08-26: Transcription models

On August 26, 2026, we notified developers using `whisper-1`, `gpt-4o-transcribe`, `gpt-4o-mini-transcribe`, and `gpt-4o-transcribe-diarize` of their deprecation and removal from the API on February 26, 2027.

For information about the recommended replacements, see the [transcription guide](https://developers.openai.com/api/docs/guides/transcription).

| Shutdown date | Model / system              | Recommended replacement                   |
| ------------- | --------------------------- | ----------------------------------------- |
| Feb 26, 2027  | `whisper-1`                 | `gpt-live-transcribe` or `gpt-transcribe` |
| Feb 26, 2027  | `gpt-4o-transcribe`         | `gpt-live-transcribe` or `gpt-transcribe` |
| Feb 26, 2027  | `gpt-4o-mini-transcribe`    | `gpt-live-transcribe` or `gpt-transcribe` |
| Feb 26, 2027  | `gpt-4o-transcribe-diarize` | `gpt-live-transcribe` or `gpt-transcribe` |

### 2026-07-20: Legacy audio, realtime, and transcription models

On July 20, 2026, we notified developers using legacy audio, realtime, and transcription model families and snapshots of their deprecation and removal from the API on January 20, 2027.

| Shutdown date | Model family / snapshot             | Recommended replacement             |
| ------------- | ----------------------------------- | ----------------------------------- |
| Jan 20, 2027  | `gpt-realtime`                      | `gpt-realtime-2.1`                  |
| Jan 20, 2027  | `gpt-audio`                         | `gpt-audio-1.5`                     |
| Jan 20, 2027  | `gpt-4o-audio`                      | `gpt-audio-1.5`                     |
| Jan 20, 2027  | `gpt-4o-realtime`                   | `gpt-realtime-2.1`                  |
| Jan 20, 2027  | `gpt-realtime-mini`                 | `gpt-realtime-2.1-mini`             |
| Jan 20, 2027  | `gpt-audio-mini`                    | `gpt-audio-1.5`                     |
| Jan 20, 2027  | `gpt-4o-mini-realtime`              | `gpt-realtime-2.1-mini`             |
| Jan 20, 2027  | `gpt-4o-mini-audio`                 | `gpt-audio-1.5`                     |
| Jan 20, 2027  | `gpt-4o-mini-transcribe-2025-03-20` | `gpt-4o-mini-transcribe-2025-12-15` |

### 2026-06-11: GPT-5 and o3 model deprecations

On June 11, 2026, we notified developers using older GPT-5 and o3 model snapshots of their deprecation and removal from the API on December 11, 2026.

| Shutdown date | Model / system          | Recommended replacement               |
| ------------- | ----------------------- | ------------------------------------- |
| Dec 11, 2026  | `gpt-5-2025-08-07`      | `gpt-5.6-sol`                         |
| Dec 11, 2026  | `gpt-5-mini-2025-08-07` | `gpt-5.6-terra`                       |
| Dec 11, 2026  | `gpt-5-nano-2025-08-07` | `gpt-5.6-luna`                        |
| Dec 11, 2026  | `gpt-5-pro-2025-10-06`  | `gpt-5.6-sol` (`reasoning.mode: pro`) |
| Dec 11, 2026  | `o3-2025-04-16`         | `gpt-5.6-sol`                         |
| Dec 11, 2026  | `o3-pro-2025-06-10`     | `gpt-5.6-sol` (`reasoning.mode: pro`) |

### 2026-06-03: Reusable prompts

On June 3, 2026, we notified developers using reusable prompts in the dashboard and API that reusable prompt objects are being deprecated.

| Date         | Update                                                                       |
| ------------ | ---------------------------------------------------------------------------- |
| June 3, 2026 | Deprecation announced and prompt creation de-emphasized in the platform.     |
| Nov 30, 2026 | The `v1/prompts` API and reusable prompt objects are scheduled to shut down. |

To migrate, move reusable prompt content into your application code. See [Migrate from prompt objects](https://developers.openai.com/api/docs/guides/prompting/migrate-from-prompt-object).

### 2026-06-03: Evals platform

On June 3, 2026, we notified developers using the Evals platform that the product is being deprecated.

| Date         | Update                                                  |
| ------------ | ------------------------------------------------------- |
| June 3, 2026 | Deprecation announced for the Evals platform.           |
| Oct 31, 2026 | Existing evals become read-only.                        |
| Nov 30, 2026 | The Evals dashboard and API are scheduled to shut down. |

Graders documented for eval workflows are part of this transition. Fine-tuning-related timelines remain covered in the self-serve fine-tuning section below.

See [Moving from OpenAI Evals to Promptfoo](https://developers.openai.com/cookbook/examples/evaluation/moving-from-openai-evals-to-promptfoo) for a migration path.

### 2026-06-03: Agent Builder

On June 3, 2026, we notified developers using Agent Builder that the product is being deprecated. ChatKit remains available.

| Date         | Update                                   |
| ------------ | ---------------------------------------- |
| June 3, 2026 | Deprecation announced for Agent Builder. |
| Nov 30, 2026 | Agent Builder is scheduled to shut down. |

See [Migrate from Agent Builder](https://developers.openai.com/api/docs/guides/agent-builder/migrate-from-agent-builder) to continue with the Agents SDK or ChatGPT Workspace Agents.

### 2026-06-02: GPT Image model deprecations

On June 2, 2026, we notified developers using older GPT Image models of their deprecation and removal from the API on December 1, 2026.

| Shutdown date | Model / system         | Recommended replacement |
| ------------- | ---------------------- | ----------------------- |
| Dec 1, 2026   | `gpt-image-1-mini`     | `gpt-image-2`           |
| Dec 1, 2026   | `gpt-image-1.5`        | `gpt-image-2`           |
| Dec 1, 2026   | `chatgpt-image-latest` | `gpt-image-2`           |

### Update to OpenAI’s self-serve fine-tuning

On May 7th, 2026, we notified developers using OpenAI’s self-serve fine-tuning platform of updates to availability.

Inference on fine-tuned models will continue to be available until the base models are deprecated.

| Date         | Update                                                                                                                                                                                           |
| ------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| May 7, 2026  | Creating fine-tuning jobs or training is not available to organizations that have not previously run fine-tuning.                                                                                |
| July 2, 2026 | Creating fine-tuning jobs is no longer available to organizations that have not run inference on a fine-tuned model in the past 60 days.                                                         |
| Jan 6, 2027  | Active existing customers will no longer be able to create new fine-tuning jobs on this date. Inference on fine-tuned models will be disabled only when the underlying base model is deprecated. |

### 2026-04-22: Legacy GPT model snapshots

To improve reliability and make it easier for developers to choose the right models, we are deprecating a set of older OpenAI models. Access to these models will be shut down on the dates below.

| Shutdown date    | Model snapshot                                                         | Substitute model                      |
| ---------------- | ---------------------------------------------------------------------- | ------------------------------------- |
| October 23, 2026 | `gpt-3.5-turbo-0125` \| `gpt-3.5-turbo`, `gpt-3.5-turbo-completions`   | `gpt-5.6-terra`                       |
| October 23, 2026 | `gpt-4-0613` \| `gpt-4`, `gpt-4-0613-completions`, `gpt-4-completions` | `gpt-5.6-sol`                         |
| October 23, 2026 | `gpt-4-1106-preview`                                                   | `gpt-5.6-sol`                         |
| October 23, 2026 | `gpt-4-turbo` \| `gpt-4-turbo-2024-04-09`, `gpt-4-turbo-completions`   | `gpt-5.6-sol`                         |
| October 23, 2026 | `gpt-4.1-nano` \| `gpt-4.1-nano-2025-04-14`                            | `gpt-5.6-luna`                        |
| October 23, 2026 | `gpt-4o-2024-05-13`                                                    | `gpt-5.6-sol`                         |
| October 23, 2026 | `gpt-image-1`                                                          | `gpt-image-2`                         |
| October 23, 2026 | `o1-2024-12-17` \| `o1`                                                | `gpt-5.6-sol`                         |
| October 23, 2026 | `o1-pro-2025-03-19` \| `o1-pro`                                        | `gpt-5.6-sol` (`reasoning.mode: pro`) |
| October 23, 2026 | `o3-mini-2025-01-31` \| `o3-mini`                                      | `gpt-5.6-sol`                         |
| October 23, 2026 | `ft-o4-mini-2025-04-16`                                                | `gpt-5.6-terra`                       |
| October 23, 2026 | `o4-mini-2025-04-16` \| `o4-mini`                                      | `gpt-5.6-terra`                       |

We are also removing fine-tuned versions as below:

| Shutdown date    | Model snapshot               | Recommended replacement base model |
| ---------------- | ---------------------------- | ---------------------------------- |
| October 23, 2026 | `ft-gpt-3.5-turbo`           | `gpt-5.6-terra`                    |
| October 23, 2026 | `ft-gpt-4`                   | `gpt-5.6-sol`                      |
| October 23, 2026 | `ft-gpt-4.1-nano-2025-04-14` | `gpt-5.6-luna`                     |
| October 23, 2026 | `ft-babbage-002`             | `gpt-5.6-terra`                    |
| October 23, 2026 | `ft-davinci-002`             | `gpt-5.6-terra`                    |

### 2026-03-24: Sora 2 video generation models and Videos API

On March 24th, 2026, we notified developers using the Videos API and Sora 2 video generation model aliases and snapshots of their deprecation and removal from the API on September 24, 2026.

| Shutdown date | Model / system          | Recommended replacement |
| ------------- | ----------------------- | ----------------------- |
| 2026-09-24    | Videos API              | ---                     |
| 2026-09-24    | `sora-2`                | ---                     |
| 2026-09-24    | `sora-2-pro`            | ---                     |
| 2026-09-24    | `sora-2-2025-10-06`     | ---                     |
| 2026-09-24    | `sora-2-2025-12-08`     | ---                     |
| 2026-09-24    | `sora-2-pro-2025-10-06` | ---                     |

### 2025-09-26: Legacy GPT model snapshots

To improve reliability and make it easier for developers to choose the right models, we are deprecating a set of older OpenAI models with declining usage over the next six to twelve months. Access to these models will be shut down on the dates below.

| Shutdown date | Model / system           | Recommended replacement |
| ------------- | ------------------------ | ----------------------- |
| 2026-09-28    | `gpt-3.5-turbo-instruct` | `gpt-5.6-terra`         |
| 2026-09-28    | `babbage-002`            | `gpt-5.6-terra`         |
| 2026-09-28    | `davinci-002`            | `gpt-5.6-terra`         |
| 2026-09-28    | `gpt-3.5-turbo-1106`     | `gpt-5.6-terra`         |

## Past deprecations

Past deprecations are listed below, with the most recent announcements at the top.

### 2026-05-08: gpt-5.2-chat-latest and gpt-5.3-chat-latest model snapshots

On May 8th, 2026, we notified developers using `gpt-5.2-chat-latest` and `gpt-5.3-chat-latest` model snapshots of their deprecation and removal from the API.

| Shutdown date | Model / system        | Recommended replacement |
| ------------- | --------------------- | ----------------------- |
| Aug 10, 2026  | `gpt-5.2-chat-latest` | `gpt-5.6-sol`           |
| Aug 10, 2026  | `gpt-5.3-chat-latest` | `gpt-5.6-sol`           |

### 2026-04-22: Legacy GPT model snapshots (July 2026 shutdown)

On April 22, 2026, we announced the deprecation of the following older OpenAI models. Access to these models was shut down on July 23, 2026.

| Shutdown date | Model snapshot                                                | Substitute model        |
| ------------- | ------------------------------------------------------------- | ----------------------- |
| July 23, 2026 | `computer-use-preview-2025-03-11` \| `computer-use-preview`   | `gpt-5.6-terra`         |
| July 23, 2026 | `gpt-4o-mini-search-preview-2025-03-11`                       | `gpt-5.6-terra`         |
| July 23, 2026 | `gpt-4o-search-preview-2025-03-11`                            | `gpt-5.6-terra`         |
| July 23, 2026 | `gpt-5-chat-latest`                                           | `gpt-5.6-sol`           |
| July 23, 2026 | `gpt-5-codex`                                                 | `gpt-5.6-sol`           |
| July 23, 2026 | `gpt-5.1-chat-latest`                                         | `gpt-5.6-sol`           |
| July 23, 2026 | `gpt-5.1-codex`                                               | `gpt-5.6-sol`           |
| July 23, 2026 | `gpt-5.1-codex-max`                                           | `gpt-5.6-sol`           |
| July 23, 2026 | `gpt-5.1-codex-mini`                                          | `gpt-5.6-terra`         |
| July 23, 2026 | `gpt-audio-mini-2025-10-06`                                   | `gpt-audio-1.5`         |
| July 23, 2026 | `gpt-realtime-mini-2025-10-06`                                | `gpt-realtime-2.1-mini` |
| July 23, 2026 | `o3-deep-research-2025-06-26` \| `o3-deep-research`           | `gpt-5.6-sol`           |
| July 23, 2026 | `o4-mini-deep-research-2025-06-26` \| `o4-mini-deep-research` | `gpt-5.6-sol`           |
| July 23, 2026 | `gpt-5.2-codex`                                               | `gpt-5.6-sol`           |


<!-- ... sections from 2025-11-18 back to 2024-10-02 removed from the capture ... -->

### 2024-08-29: Fine-tuning training on babbage-002 and davinci-002 models

On August 29th, 2024, we notified developers fine-tuning `babbage-002` and `davinci-002` that new fine-tuning training runs on these models will no longer be supported starting October 28, 2024.

Fine-tuned models created from these base models are not affected by this deprecation, but you will no longer be able to create new fine-tuned versions with these models.

| Shutdown date | Model / system                            | Recommended replacement |
| ------------- | ----------------------------------------- | ----------------------- |
| 2024-10-28    | New fine-tuning training on `babbage-002` | `gpt-4o-mini`           |
| 2024-10-28    | New fine-tuning training on `davinci-002` | `gpt-4o-mini`           |


<!-- ... older sections removed from the capture ... -->
