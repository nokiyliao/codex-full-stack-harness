use lifecycle::SessionManagement;

use super::runtime_prompt_manual::active_manual_display_names;

pub fn self_reflection_tail_prompt(session: &SessionManagement) -> String {
    let manuals = active_manual_display_names(session);
    let manual_list = if manuals.is_empty() {
        "active Operation Manual(s)".to_string()
    } else {
        manuals.join(", ")
    };

    format!(
        "Before the next `command_run` batch or final answer, review the {manual_list} and complete the required `Self Reflection` against the user's goal. If it reveals a mistake or incomplete chain, correct that before continuing."
    )
}
