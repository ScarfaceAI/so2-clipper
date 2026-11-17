# Project terms

This glossary defines terms used in the public documentation and Skill workflow.

| Term | Meaning |
|---|---|
| **Current viewpoint** | The player whose actions the recording currently shows. In spectator footage, this is the player selected in the spectator list, identified from the eye marker and supporting visual evidence. |
| **Anonymous identity** | A stable visual identifier used when a name is unreadable but continuous viewpoint attribution is supported. It can retain a confirmed visual segment; it does not satisfy a request to filter for a named player. |
| **Candidate** | An event or clip range proposed by the scanner for review. A candidate is not a confirmed kill or a final clip decision. |
| **Evidence frame** | A still image extracted from a source recording or rendered clip at a recorded time, used to inspect an event or boundary. Source and output evidence are kept distinct. |
| **Warmup** | The pre-match practice phase. Events confirmed to occur in warmup are not counted as match kills. |
| **Review queue** | Candidate events or source intervals that need visual confirmation, including possible missed events or unclear identity and timing. |
| **Full-round clip** | A reviewed clip range covering confirmed kills for one viewpoint within one actual round. Candidate grouping alone does not establish a full round. |
| **Weapon category** | The folder classification assigned from the weapon used for each confirmed kill. A batch's category folders are based on the reviewed kill list. |
| **Run** | One tool invocation with its own identifier, receipt, and task state. A receipt describes that invocation and must not be used as proof for a different run. |
| **Runtime snapshot** | The generated copy of engine scripts included in the standalone Skill ZIP so that the installed Skill can run without the development checkout. |

For implementation ownership and generated files, see [SOURCE_OF_TRUTH.md](SOURCE_OF_TRUTH.md).
