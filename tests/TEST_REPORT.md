# Test report

Fukuhara Research Seminar Dashboard, v1. Produced on 2026-10-08 (updated after moving the project to ~/Desktop/Zemi/3-3/dashboard/v1/).

## 1. The tests

All tests use an in-memory SQLite database, a temporary folder for files, and test values for every secret (see `tests/conftest.py`). They never touch `local.db`, `local_files/` or `.env`.

### `test_auth.py` (36 tests)

| Test | What it checks |
|---|---|
| `test_gens_1_to_12_exist_before_anyone_registers` | Gen 1–12 are created at startup, before any account exists. |
| `test_register_page_lists_gens_newest_first` | The registration Gen dropdown lists every gen, newest first. |
| `test_database_url_scheme_is_converted` | `postgres://` and `postgresql://` become `postgresql+psycopg://`; SQLite URLs are untouched. |
| `test_add_missing_columns_adds_new_fields_with_their_defaults` | `add_missing_columns()` adds a column missing from an older table, fills its default, and then has nothing left to add. |
| `test_passwords_are_stored_as_salted_scrypt_hashes` | Passwords are scrypt hashes with a random salt (same password, different hashes) and verify correctly. |
| `test_student_and_admin_register_with_their_own_keys` | Students and admins register with their own key; Japanese names work; students get guest number 1. |
| `test_wrong_or_missing_key_is_refused` | A wrong, empty or other-role key is refused and creates no account. |
| `test_unset_key_switches_registration_off` | An unset admin key switches admin registration off. |
| `test_identical_keys_are_refused` | If both keys are set to the same value, registration is refused. |
| `test_student_must_choose_a_gen` | A student registration without a gen is refused. |
| `test_login_id_rules_and_duplicates_in_any_case` | Login ID length/character rules; a duplicate Login ID in different letter case is refused. |
| `test_password_rules` | Passwords need 8+ characters and both copies must match. |
| `test_login_needs_no_key_and_uses_the_login_id` | Login takes only Login ID (any case) and password, and opens /classes. |
| `test_login_failure_never_says_which_part_was_wrong` | A wrong password and an unknown Login ID give the same message. |
| `test_signed_out_browser_goes_to_login_and_comes_back` | A signed-out browser is sent to /login with `next` set; `/` goes to /login. |
| `test_root_goes_to_classes_when_signed_in` | `/` goes to /classes for a signed-in viewer (including guests). |
| `test_pages_are_never_stored_by_browsers` | Pages are sent with `Cache-Control: no-store`. |
| `test_cookie_is_httponly_and_lax` | The sign-in cookie is HttpOnly, SameSite=Lax and lasts 7 days. |
| `test_logout_ends_only_this_device` | Log out ends this device only; another device stays signed in. |
| `test_sign_out_all_devices` | "Sign out all devices" ends every device of that member. |
| `test_expired_sign_in_no_longer_works` | An expired sign-in is no longer accepted. |
| `test_guest_visit_lasts_one_day` | A guest visit's cookie lasts 1 day. |
| `test_password_change_signs_other_devices_out` | Changing the password needs the current one, keeps this device, signs the others out, and the new password works. |
| `test_rate_limit_after_ten_failures` | After 10 failed logins from one address, login and registration are refused (429); another address is unaffected. |
| `test_wrong_registration_keys_count_towards_the_rate_limit` | Wrong registration keys count towards the same limit. |
| `test_post_without_csrf_token_gets_403` | A POST without (or with a wrong) CSRF token gets 403. |
| `test_csrf_token_in_a_plain_form_field_works` | The CSRF token also works as a hidden `csrf_token` form field. |
| `test_account_page_is_for_members_only` | Guests can't open the Account page; members see their name and Login ID. |
| `test_other_names_are_limited_to_ten` | More than 10 other names are refused. |
| `test_full_database_gives_a_friendly_page_and_toast` | When the database is read-only (full), pages and HTMX buttons get a plain "database is full" message (503), not "Internal Server Error". |
| `test_unexpected_errors_get_a_plain_message` | Any other unexpected error shows "Something went wrong" (500). |
| `test_agent_api_errors_are_503_so_the_connector_retries` | The agent API answers 503 with the message, so connectors buffer and retry. |
| `test_row_level_security_is_switched_on_for_owned_tables_only` | On Postgres, row level security is switched on for the site's tables that this connection owns and that don't have it yet (Supabase's "RLS disabled" warnings); checked with a stand-in connection. |
| `test_row_level_security_does_nothing_on_sqlite` | On SQLite (no RLS) nothing is changed. |
| `test_pages_and_stylesheets_are_compressed_and_cached` | Pages and stylesheets are gzip-compressed; fingerprinted static files may be cached for a year, pages never. |
| `test_downloads_are_not_compressed_again` | Downloads such as PDFs are sent as they are, not compressed again. |

### `test_classes.py` (24 tests)

| Test | What it checks |
|---|---|
| `test_class_visibility_for_every_kind_of_viewer` | Admin, enrolled student, student hidden by gen, hidden by name, enrolled-and-hidden (enrolment wins), other student, guest with and without "Hidden from guests"; hidden classes also leave the timeline. |
| `test_gen_enrolment_includes_students_who_register_later` | A student who registers later into an enrolled gen is enrolled (and gets a cell), even though that gen is also hidden. |
| `test_hidden_class_looks_like_a_missing_one` | A hidden class (and its pages) answer 404, like a missing one. |
| `test_only_admins_create_edit_and_delete_classes` | Students and guests can't create, edit or delete classes; admins can; delete needs the admin's password and removes link rows. |
| `test_class_form_checks_its_fields` | The class form needs a description and an end month not before the start. |
| `test_automatic_label_when_there_is_no_title` | A class without a title is called "Apr 2026 – Sep 2026" (or one month, "Apr 2026"). |
| `test_class_page_shows_info_tabs_and_enrolment_summary` | The class page shows "Gen 9, Gen 10 + 1 student", the three tabs, admin buttons; tab fragments load alone. |
| `test_overlapping_classes_never_share_a_lane` | Two overlapping classes go in lane +1 and lane −1. |
| `test_three_or_more_overlapping_classes_add_lanes` | Five overlapping classes get five different lanes (+1, −1, +2, −2, +3). |
| `test_non_overlapping_classes_reuse_lanes` | Classes that don't overlap reuse lane +1. |
| `test_timeline_boxes_never_overlap_at_any_zoom` | At every zoom level, boxes in one lane never overlap and are at least the minimum width. |
| `test_timeline_page_and_zoom` | The timeline page shows "Today (JST)" and escaped titles; the zoom fragment returns the new level out of band. |
| `test_enrolled_and_ended_badges_on_the_timeline` | "Enrolled" and "Ended" badges appear on the timeline. |
| `test_times_are_shown_in_jst` | UTC times are shown as "2026-10-08 07:10 JST"; JST form input is stored as UTC. |
| `test_relative_times` | Relative times read "in 2 days", "3 hours ago", "just now". |
| `test_html_in_class_text_is_escaped` | HTML in class titles and descriptions is escaped. |
| `test_timeline_reaches_back_to_the_oldest_class_and_three_years_forward` | The timeline starts 12 months back or at the oldest class (whichever is earlier) and reaches 3 years forward, or further for a class that ends later. |
| `test_a_tick_on_every_friday_on_its_exact_date` | There is a tick on every Friday (4–5 a month), each on its exact date, kept clear of the month borders. |
| `test_classes_have_the_tabs_ticked_at_creation` | A class shows Assignments, plus Exams and/or Agents only if they were ticked: three, two or one tab. |
| `test_switched_off_tabs_and_their_pages_are_not_found` | A switched-off tab's pages, forms and refreshes are 404, new exam submissions and agents are refused, and ?tab= falls back to Assignments. |
| `test_switching_a_tab_off_hides_its_data_and_on_brings_it_back` | Switching tabs off hides their exams, files and agent pages (the agent itself keeps running); switching them back on restores everything. |
| `test_the_class_form_has_the_tabs_ticked_by_default` | The class form starts with Exams and Agents ticked. |
| `test_existing_classes_get_all_three_tabs` | Classes created before the switches existed get both switched on when the columns are added. |
| `test_hidden_exam_submissions_dont_mark_the_class_as_new` | Exam submissions in a class with Exams switched off don't put a notification mark on it. |

### `test_coursework.py` (45 tests)

| Test | What it checks |
|---|---|
| `test_only_admins_post_edit_and_delete_tasks` | Students and guests can't post, edit or delete assignments; admins can. |
| `test_assignments_and_exams_are_kept_apart` | Exams and assignments are separate tabs; an unknown tab is 404. |
| `test_task_needs_title_due_time_and_content` | A task needs a title, a due time, and text or a file. |
| `test_task_with_a_file_keeps_exactly_one_kind_of_content` | A task holds a file or text, never both; switching to text deletes the old file. |
| `test_due_indicator` | "Due soon" within 72 hours, nothing beyond, "Past due" once passed. |
| `test_bar_shows_the_latest_task_with_its_indicator` | The bar shows the latest task with JST time and indicator; only admins get the "+". |
| `test_polling_answers_204_when_nothing_changed` | A self-refreshing section gets 204 when its content hasn't changed. |
| `test_enrolled_student_submits_and_it_appears_everywhere` | A linked submission appears on the history page, in the student's cell, and the student sees Create/Edit and "You". |
| `test_only_enrolled_students_can_submit` | Non-enrolled students, admins and guests can't submit. |
| `test_only_the_owner_edits_or_deletes_a_submission` | Only the student who made a submission can edit or delete it; editing sets edited_at and replacing a file deletes the old one. |
| `test_admins_can_delete_any_submission` | Admins can delete any submission (moderation). |
| `test_link_only_to_a_task_of_the_same_class_and_kind` | A submission can only link to a task of the same class and kind. |
| `test_submission_without_a_task_is_fine` | A submission with no linked task is accepted. |
| `test_submission_content_is_text_or_file` | Submission content must be text or a file, exactly one. |
| `test_late_badge` | "Late" shows when submitted after the due time, not before. |
| `test_dropdown_lists_five_recent_tasks_then_older` | The task dropdown lists the 5 newest, with older ones behind "Show older". |
| `test_file_download_headers` | Non-image files download as attachment, octet-stream, with nosniff. |
| `test_images_may_show_inline` | PNG images are served inline with their type. |
| `test_files_are_kept_in_file_storage_not_the_database` | Uploaded bytes are in file storage (the local folder), not the database. |
| `test_deleting_a_submission_deletes_its_file` | Deleting a submission deletes its stored file. |
| `test_file_size_limit` | A file over the limit is refused and nothing is stored. |
| `test_uploads_stop_when_storage_is_nearly_full` | When stored files would exceed the total cap, uploads are refused with "ask an admin". |
| `test_file_permissions` | `/files/{id}` checks permissions: hidden class → 404, unticked file for guests → 404. |
| `test_a_submission_can_have_several_files` | One submission takes several files, kept in order; all are listed and downloadable. |
| `test_editing_removes_ticked_files_and_adds_new_ones` | Editing removes the ticked files (from storage too) and adds new ones; removing every file without adding one is refused. |
| `test_tasks_can_have_several_files_too` | Assignments take several files; deleting the assignment deletes all of them. |
| `test_one_refused_file_stores_none_of_them` | If one file of an upload is too big, none are kept (the ones already stored are removed again). |
| `test_at_most_20_files` | More than 20 files on one item are refused. |
| `test_old_single_files_move_to_attachments` | An item's file saved the old way (`file_id`) moves to the Attachment table at start-up, only once. |
| `test_guests_cannot_download_attached_files_unless_ticked` | Guests get 404 for every file of an unticked submission, and the file of a ticked one. |
| `test_text_content_is_shown_as_plain_text` | Text submissions and titles containing HTML are escaped. |
| `test_deleting_a_class_deletes_its_files` | Deleting a class deletes its submissions and their files. |
| `test_deleting_a_task_keeps_its_submissions` | Deleting a task keeps its submissions, unlinked. |
| `test_due_time_is_entered_and_shown_in_jst` | A due time typed in JST is stored correctly and shown in JST. |
| `test_new_submission_marks_the_class_until_its_page_is_opened` | Someone else's new submission puts a mark on the class's timeline box for each other member until they open the class; never for your own. |
| `test_new_members_only_see_marks_for_submissions_after_they_registered` | New accounts only get marks for submissions made after they registered. |
| `test_guests_get_no_notification_marks` | Guests never get notification marks. |
| `test_supabase_keys_are_sent_the_way_each_kind_needs` | New `sb_secret_` keys go only in the `apikey` header; legacy `service_role` JWTs are also sent as a bearer token. |
| `test_files_go_to_supabase_storage_and_back` | Against a stand-in Supabase server: a 3 MB file is uploaded whole (right address, type and headers, even when SUPABASE_URL ends in /rest/v1), downloads identically, and is deleted with its submission. |
| `test_supabase_refusals_are_explained` | When Supabase refuses (wrong bucket, wrong key), the form and the Members page show Supabase's own reason, and nothing is half-saved. |
| `test_storage_check_warns_about_render_without_supabase` | On Render without Supabase settings, the Members page warns that files are on a temporary disk and will be lost. |
| `test_a_missing_file_gives_a_clear_message` | A file missing from storage gives a clear 404 message instead of a broken download. |
| `test_pdfs_html_and_text_can_be_previewed` | PDFs preview in the browser's viewer; HTML and text preview under a sandbox policy with no scripts; other files stay downloads; the page shows Preview buttons. |
| `test_a_fake_pdf_is_not_previewed` | A file that only claims to be a PDF isn't previewed. |
| `test_previews_follow_download_permissions` | Previews have the same permissions as downloads (an unticked file is 404 for guests). |

### `test_agents.py` (46 tests)

| Test | What it checks |
|---|---|
| `test_only_the_owner_gets_add_agent_controls_settings_and_inbox` | Only the owner sees Add Agent, Implement connector, folder editing, controls, settings and Inbox; others get a read-only view. |
| `test_owner_only_routes_refuse_everyone_else` | Every owner-only route (commands, rename, description, monitor, pins, keys, delete, Inbox) answers 403 to another student and to an admin. |
| `test_members_can_read_other_agents_but_guests_only_see_the_grid` | Members can read other agents' pages; guests are refused agent pages and fragments, but see the grid with masked names. |
| `test_non_owners_cannot_post_controls_as_guests` | Guests can't send commands or set bells. |
| `test_agents_tab_shows_last_connection` | The Agents tab cell shows "Never connected", then "Last connected … JST" and online. |
| `test_blank_key_generates_one_and_typed_keys_follow_the_rules` | A blank key is generated (and works); typed keys need 20+ characters, no spaces, and must be unused. |
| `test_key_is_stored_hashed_and_encrypted_and_can_be_viewed` | Keys are stored as a fingerprint plus encrypted copy; View key needs the owner's password and isn't cached. |
| `test_replace_key_stops_the_old_one` | Replace key: the old key gets 401, the new one works. |
| `test_renaming_keeps_the_key` | Renaming keeps the key; a name already used by the owner in the class is refused. |
| `test_names_are_unique_per_owner_per_class` | Another student or another class may reuse an agent name. |
| `test_delete_agent_needs_the_password_and_deletes_everything` | Deleting an agent needs the owner's password and removes its outputs, activity and files. |
| `test_unknown_empty_or_missing_keys_get_401` | Unknown, empty or missing API keys get 401. |
| `test_check_in_every_limits` | `check_in_every` outside 1 s–1 week is 422; a valid value is stored. |
| `test_online_status_uses_the_interval_plus_slack` | Online = last seen within the interval plus half of it (at least 30 s). |
| `test_late_items_land_at_their_real_time` | Late `created_at` is kept and marked "(delivered late)"; future times become now; bad times are 422. |
| `test_late_monitor_update_older_than_the_current_one_is_skipped` | A late update older than a monitor's current value is skipped. |
| `test_commands_start_pause_and_messages` | Pause/Start set the paused state; messages are refused (409) until the agent opts in; commands are listed and marked done. |
| `test_agents_only_see_and_update_their_own_commands` | An agent can't see or update another agent's commands. |
| `test_messages_ready_check_reloads_the_page_when_it_changes` | The page reloads only when the agent's message opt-in changes. |
| `test_inbox_is_refused_until_opted_in_and_confirming_deletes_the_file` | Inbox uploads are refused until opted in; the agent lists, downloads and confirms; confirming deletes the stored file. |
| `test_inbox_files_are_only_for_that_agent` | Other agents and students can't see or take an agent's Inbox files. |
| `test_output_kinds_and_file_limits` | Outputs are typed text/number/link/json; bad categories, over-long text, oversize files and non-PDF monitors are refused. |
| `test_monitor_pdf_is_shown_inline_and_replaced` | A monitor PDF is shown inline, replaced (old file deleted) and kept in its viewer while the list refreshes; a fake PDF stays a plain download. |
| `test_reading_by_me_id_and_name_within_the_class` | Agents read by `me`, id or name, only within their class (others 404), never seeing keys or Login IDs. |
| `test_shared_name_in_a_class_gets_409_with_ids` | Reading a name shared by two agents in the class gives 409 with their ids. |
| `test_reading_outputs_and_activity_with_filters` | Output/activity reads filter by category, type, after and before; limit is capped at 500. |
| `test_file_outputs_can_be_downloaded_by_agents_in_the_class` | Agents in the class can download another agent's file output (as attachment). |
| `test_agents_delete_only_their_own_data` | Deleting another agent's output or activity is 403; own deletes work, need a filter or all=true, and leave "delete" lines. |
| `test_reads_never_include_commands_or_inbox` | Agent reads never expose commands sent to an agent. |
| `test_outputs_filter_pin_and_bell` | Outputs: nothing shows until chosen; a pinned category starts ticked and shows on the card; bells are per viewer. |
| `test_connector_holds_outputs_while_paused_and_sends_them_on_start` | The real connector holds outputs while paused (reports still go through) and sends them on Start with their original time, marked late. |
| `test_connector_opts_in_to_messages_and_inbox` | The real connector's accept_messages and accept_inbox deliver a message and an Inbox file, and confirm it. |
| `test_each_agent_keeps_only_the_newest_outputs_and_activity` | Past the cap, an agent's oldest outputs (with their files) and Activity lines are deleted. |
| `test_an_agent_has_many_monitors_each_replaced_not_added` | An agent has many named monitors of any kind; sending a name again replaces its value; creating and removing are logged, updates aren't; null removes; names are 1–50 characters. |
| `test_monitors_have_the_same_filter_pins_and_bells_as_outputs` | Monitors have the outputs' Filter (choose, sort), per-viewer bells and the pin; the pin is shared with outputs, starts ticked and shows the live value on the card; only the owner pins. |
| `test_owner_sets_and_removes_link_monitors_in_settings` | The owner adds link monitors by name in settings, and removes one by saving an empty link; non-http links are refused. |
| `test_agents_read_monitors_of_their_class_only` | Agents read the monitors of agents in their class (all, or by name, with file downloads); other classes are 404. |
| `test_monitor_cap_per_agent` | An agent can have at most 100 monitors (a new 101st is 422; replacing is always allowed). |
| `test_older_connectors_single_monitor_still_works` | The earlier single-monitor endpoints still work, as the monitor called "Monitor". |
| `test_a_legacy_single_monitor_is_moved_into_the_monitor_table` | A monitor saved the old way is moved into the Monitor table at startup (once), keeping its pin. |
| `test_deleting_an_agent_deletes_its_monitors_and_their_files` | Deleting an agent deletes its monitors and their files. |
| `test_connector_sets_named_monitors_and_buffers_only_the_newest_value_each` | The real connector sets named monitors of each kind, holds them while paused keeping only each monitor's newest value, reads, downloads and removes them. |
| `test_filter_choices_are_remembered_per_member_agent_and_feed` | Each member's Filter choices (ticks, All, sort) are saved per agent and per feed and are back on the next visit; other members start from the owner's choices until they choose their own, which don't change the owner's. |
| `test_background_refreshes_without_the_filter_change_nothing` | Refreshes that don't carry the Filter, or carry the same choices, don't rewrite the saved choices. |
| `test_a_non_owners_choices_are_not_a_preset_for_others` | Only the owner's choices are a starting point for others; another member's choices aren't. |
| `test_connector_shares_reads_and_trees_skills` | The real connector shares, updates, reads (always the latest), lists, downloads and deletes skills, and sets and reads a skill tree. |

### `test_guests.py` (12 tests)

| Test | What it checks |
|---|---|
| `test_placeholders_are_fixed_and_never_reused` | Placeholders are "Gen 9 Student N" by registration order per gen, never reused after a deletion; admins keep their names. |
| `test_name_search_catches_parts_case_widths_and_other_names` | The name search catches full names, parts, other case, full-width letters, Japanese without spaces and other names; Latin parts must stand alone. |
| `test_members_always_see_the_original_text` | Members see the original text, unmasked. |
| `test_guests_never_see_a_real_name_or_login_id_anywhere` | Across every guest-visible page, no real name, name part, other name or Login ID appears; renames apply immediately. |
| `test_unticked_items_do_not_exist_for_guests` | Unticked tasks and submissions are absent for guests and their files are 404. |
| `test_ticked_submissions_are_shown_in_full` | A ticked submission is shown to guests with its file. |
| `test_ticked_submission_linked_to_a_hidden_task_shows_no_link` | A ticked submission linked to an unticked task shows no link to guests. |
| `test_class_hidden_from_guests_hides_everything_in_it` | A class hidden from guests hides even ticked items and files. |
| `test_guest_cell_shows_the_latest_guest_visible_submission` | A guest's cell shows the latest guest-visible submission only. |
| `test_visible_to_guests_checkbox_warns_about_files` | The "Visible to guests" checkbox warns that names inside files are not hidden. |
| `test_guests_are_refused_on_every_upload_edit_and_control_route` | Guests get 403 on every create, edit, delete, upload and control route, and see no such buttons. |
| `test_guests_never_see_the_members_page_or_login_ids` | Guests and students can't open the Members page; admins see Login IDs and placeholders there. |

### `test_members.py` (13 tests)

| Test | What it checks |
|---|---|
| `test_members_page_groups_accounts_and_shows_storage` | The Members page groups admins and gens (newest first) and shows storage use. |
| `test_admin_changes_name_and_gen` | Admins change a student's name and gen (with a new placeholder number in the new gen). |
| `test_reset_password_shows_it_once_and_signs_the_member_out` | Reset password shows a working temporary password once and signs the member out. |
| `test_delete_account_needs_the_admin_password_and_removes_their_data` | Deleting an account needs the admin's password and removes their submissions, agents and files. |
| `test_admins_cannot_delete_themselves` | Admins can't delete their own account from the Members page. |
| `test_add_the_next_gen` | Admins add Gen 13, which then appears at registration; students can't. |
| `test_folders_belong_to_their_student_and_are_shared_across_classes` | Folders belong to one student, are shared across their classes with per-class contents, can't be used or changed by others, and deleting keeps agents. |
| `test_grid_pages_hold_nine_cells_including_add_agent` | The grid holds 9 cells per page with the owner's Add Agent card counting as one. |
| `test_members_page_shows_the_database_size` | The Members page shows the database size out of 500 MB. |
| `test_file_storage_breakdown_by_type_class_person_agent_and_largest` | The file breakdown adds up exact sizes by type, class, person and agent, and lists the largest files with what they belong to. |
| `test_pie_gives_six_colours_then_other` | The pie gives the six biggest rows their own colour (in fixed order), puts the rest in a grey "Other" slice, and its shares add up to 100%; a single row is a whole ring. |
| `test_database_breakdown_estimates_by_class_person_and_agent` | The database breakdown estimates each agent's, person's and class's share from the text they store, and says it's an estimate. |
| `test_breakdowns_are_for_admins_only` | Breakdowns load only for admins (students and guests get 403), and only when opened. |

### `test_skills.py` (28 tests)

| Test | What it checks |
|---|---|
| `test_a_member_shares_a_skill_and_it_appears_with_its_identifier` | A member shares a skill; it's listed with its identifier and credit, and its page shows the identifier, address, connector call and text. |
| `test_sharing_again_with_the_same_name_updates_it` | Sharing again with the same name updates the skill (next version, same identifier). |
| `test_skill_files_are_checked` | Empty or over-100 KB skill files are refused. |
| `test_several_skill_files_become_several_skills` | Several skill files shared at once each become a skill named after its heading, with the same description and sharing. |
| `test_one_bad_skill_file_shares_none` | If one of several skill files is refused, none are shared, and the error names the file. |
| `test_the_connector_skill_is_built_in_everywhere` | The built-in "Dashboard connector" skill is on every class's market (class view too), has no class or owner, can't be edited or removed (even by admins or through the API), and agents read it as `skill-connector` with this site's address filled in. Guests can't see it. |
| `test_the_connector_skill_follows_the_dashboard` | Restarting with the same connector text keeps its version; changed text replaces it and raises the version. |
| `test_older_databases_let_skills_have_no_class_or_owner` | An older SQLite `skill` table (class and owner required) is rebuilt so they may be empty, keeping its rows; only once. |
| `test_downloading_counts` | Downloading gives the file and counts it. |
| `test_class_only_and_seminar_wide_skills` | Class-only skills show only in their class; seminar-wide ones also in every class's Whole seminar view and in their own class; a student who can't see the origin class still sees seminar-wide skills. |
| `test_the_market_is_for_members_only` | Guests can't see, open, download or share skills, and the market isn't shown to them. |
| `test_only_its_member_edits_and_admins_can_remove` | Only the member a skill is credited to edits it; others are refused; admins can remove any skill. |
| `test_filters_search_source_and_mine` | Search, shared-by (members or agents), Mine and sorting by name work. |
| `test_agents_post_read_update_and_download_skills` | Agents post skills (credited "from X's agent, Y"), update them by name, read the latest text by identifier, download (counted) and list them. |
| `test_agents_only_delete_their_own_skills` | An agent can delete only skills it posted (403 otherwise). |
| `test_agents_see_skills_as_their_owner_does` | Agents see the market as their owner does; unknown skills are 404 and wrong keys 401. |
| `test_skill_trees` | Skill trees are set by an agent or its owner on the page (not by others), readable through the API and shown with the agent's skills. |
| `test_a_skill_tree_needs_its_ai_version_first` | A user-friendly version alone is refused until the AI version (.md) exists; both at once works. |
| `test_people_see_the_user_friendly_version_first` | With only the AI version, it's shown; after adding an HTML version, that is shown first (sealed-off preview) with the AI version behind a button; replacing it with an image deletes the old file and shows the image; the AI version is kept. |
| `test_user_friendly_versions_are_checked_and_protected` | Other file types and fake PDFs are refused; guests can't download the file but members can; only the owner removes the user-friendly version, which deletes its file and keeps the AI version. |
| `test_agents_set_the_user_friendly_version_after_the_ai_one` | Through the API: 409 before the AI version exists, then set, read (details and download), and deleted with the whole tree. |
| `test_deleting_an_agent_deletes_its_tree_files` | Deleting an agent deletes its skill tree's file from storage. |
| `test_deleting_an_agent_keeps_its_skills_credited` | Deleting an agent keeps its shared skills, still credited to it, and removes its skill tree. |
| `test_keyword_rules_sort_skills_by_function` | Without an AI key, keyword rules sort skills by function (data retrieval apart from decision frameworks); the market groups them with each category's description, or shows one category's description on top; only admins can organize now. |
| `test_the_ai_organizer_uses_one_request_and_applies_its_answer` | With a key, the organizer sends exactly one request (stand-in reply, no real call) and applies its categories, descriptions and specialties. |
| `test_a_bad_ai_answer_changes_nothing_and_the_daily_limit_holds` | An unusable AI reply changes nothing, and no more than two AI requests are made a day. |
| `test_the_schedule_always_runs_at_3pm_and_once_more_only_if_needed` | An update is due at 15:00 JST every day (also catching up later that day), and around 03:00 only if skills changed. |
| `test_the_daily_timer_needs_its_token` | The timer's organizer address needs the right ORGANIZER_TOKEN (and never matches an empty one). |

## 2. Result of `pytest -v`

Run with `.venv/bin/python -m pytest -v` (Python 3.12.3, macOS).

```text
tests/test_agents.py::test_only_the_owner_gets_add_agent_controls_settings_and_inbox PASSED [  0%]
tests/test_agents.py::test_owner_only_routes_refuse_everyone_else PASSED [  0%]
tests/test_agents.py::test_members_can_read_other_agents_but_guests_only_see_the_grid PASSED [  1%]
tests/test_agents.py::test_non_owners_cannot_post_controls_as_guests PASSED [  1%]
tests/test_agents.py::test_agents_tab_shows_last_connection PASSED       [  2%]
tests/test_agents.py::test_blank_key_generates_one_and_typed_keys_follow_the_rules PASSED [  2%]
tests/test_agents.py::test_key_is_stored_hashed_and_encrypted_and_can_be_viewed PASSED [  3%]
tests/test_agents.py::test_replace_key_stops_the_old_one PASSED          [  3%]
tests/test_agents.py::test_renaming_keeps_the_key PASSED                 [  4%]
tests/test_agents.py::test_names_are_unique_per_owner_per_class PASSED   [  4%]
tests/test_agents.py::test_delete_agent_needs_the_password_and_deletes_everything PASSED [  5%]
tests/test_agents.py::test_unknown_empty_or_missing_keys_get_401 PASSED  [  5%]
tests/test_agents.py::test_check_in_every_limits PASSED                  [  6%]
tests/test_agents.py::test_online_status_uses_the_interval_plus_slack PASSED [  6%]
tests/test_agents.py::test_late_items_land_at_their_real_time PASSED     [  7%]
tests/test_agents.py::test_late_monitor_update_older_than_the_current_one_is_skipped PASSED [  7%]
tests/test_agents.py::test_commands_start_pause_and_messages PASSED      [  8%]
tests/test_agents.py::test_agents_only_see_and_update_their_own_commands PASSED [  8%]
tests/test_agents.py::test_messages_ready_check_reloads_the_page_when_it_changes PASSED [  9%]
tests/test_agents.py::test_inbox_is_refused_until_opted_in_and_confirming_deletes_the_file PASSED [  9%]
tests/test_agents.py::test_inbox_files_are_only_for_that_agent PASSED    [ 10%]
tests/test_agents.py::test_output_kinds_and_file_limits PASSED           [ 10%]
tests/test_agents.py::test_monitor_pdf_is_shown_inline_and_replaced PASSED [ 11%]
tests/test_agents.py::test_reading_by_me_id_and_name_within_the_class PASSED [ 11%]
tests/test_agents.py::test_shared_name_in_a_class_gets_409_with_ids PASSED [ 12%]
tests/test_agents.py::test_reading_outputs_and_activity_with_filters PASSED [ 12%]
tests/test_agents.py::test_file_outputs_can_be_downloaded_by_agents_in_the_class PASSED [ 13%]
tests/test_agents.py::test_agents_delete_only_their_own_data PASSED      [ 13%]
tests/test_agents.py::test_reads_never_include_commands_or_inbox PASSED  [ 14%]
tests/test_agents.py::test_outputs_filter_pin_and_bell PASSED            [ 14%]
tests/test_agents.py::test_an_agent_has_many_monitors_each_replaced_not_added PASSED [ 15%]
tests/test_agents.py::test_monitors_have_the_same_filter_pins_and_bells_as_outputs PASSED [ 15%]
tests/test_agents.py::test_owner_sets_and_removes_link_monitors_in_settings PASSED [ 16%]
tests/test_agents.py::test_agents_read_monitors_of_their_class_only PASSED [ 16%]
tests/test_agents.py::test_monitor_cap_per_agent PASSED                  [ 17%]
tests/test_agents.py::test_older_connectors_single_monitor_still_works PASSED [ 17%]
tests/test_agents.py::test_a_legacy_single_monitor_is_moved_into_the_monitor_table PASSED [ 18%]
tests/test_agents.py::test_deleting_an_agent_deletes_its_monitors_and_their_files PASSED [ 18%]
tests/test_agents.py::test_connector_holds_outputs_while_paused_and_sends_them_on_start PASSED [ 19%]
tests/test_agents.py::test_connector_opts_in_to_messages_and_inbox PASSED [ 19%]
tests/test_agents.py::test_each_agent_keeps_only_the_newest_outputs_and_activity PASSED [ 20%]
tests/test_agents.py::test_connector_sets_named_monitors_and_buffers_only_the_newest_value_each PASSED [ 20%]
tests/test_agents.py::test_filter_choices_are_remembered_per_member_agent_and_feed PASSED [ 21%]
tests/test_agents.py::test_background_refreshes_without_the_filter_change_nothing PASSED [ 21%]
tests/test_agents.py::test_a_non_owners_choices_are_not_a_preset_for_others PASSED [ 22%]
tests/test_agents.py::test_connector_shares_reads_and_trees_skills PASSED [ 22%]
tests/test_auth.py::test_gens_1_to_12_exist_before_anyone_registers PASSED [ 23%]
tests/test_auth.py::test_register_page_lists_gens_newest_first PASSED    [ 23%]
tests/test_auth.py::test_database_url_scheme_is_converted PASSED         [ 24%]
tests/test_auth.py::test_add_missing_columns_adds_new_fields_with_their_defaults PASSED [ 24%]
tests/test_auth.py::test_passwords_are_stored_as_salted_scrypt_hashes PASSED [ 25%]
tests/test_auth.py::test_student_and_admin_register_with_their_own_keys PASSED [ 25%]
tests/test_auth.py::test_wrong_or_missing_key_is_refused PASSED          [ 25%]
tests/test_auth.py::test_unset_key_switches_registration_off PASSED      [ 26%]
tests/test_auth.py::test_identical_keys_are_refused PASSED               [ 26%]
tests/test_auth.py::test_student_must_choose_a_gen PASSED                [ 27%]
tests/test_auth.py::test_login_id_rules_and_duplicates_in_any_case PASSED [ 27%]
tests/test_auth.py::test_password_rules PASSED                           [ 28%]
tests/test_auth.py::test_login_needs_no_key_and_uses_the_login_id PASSED [ 28%]
tests/test_auth.py::test_login_failure_never_says_which_part_was_wrong PASSED [ 29%]
tests/test_auth.py::test_signed_out_browser_goes_to_login_and_comes_back PASSED [ 29%]
tests/test_auth.py::test_root_goes_to_classes_when_signed_in PASSED      [ 30%]
tests/test_auth.py::test_pages_are_never_stored_by_browsers PASSED       [ 30%]
tests/test_auth.py::test_cookie_is_httponly_and_lax PASSED               [ 31%]
tests/test_auth.py::test_logout_ends_only_this_device PASSED             [ 31%]
tests/test_auth.py::test_sign_out_all_devices PASSED                     [ 32%]
tests/test_auth.py::test_expired_sign_in_no_longer_works PASSED          [ 32%]
tests/test_auth.py::test_guest_visit_lasts_one_day PASSED                [ 33%]
tests/test_auth.py::test_password_change_signs_other_devices_out PASSED  [ 33%]
tests/test_auth.py::test_rate_limit_after_ten_failures PASSED            [ 34%]
tests/test_auth.py::test_wrong_registration_keys_count_towards_the_rate_limit PASSED [ 34%]
tests/test_auth.py::test_post_without_csrf_token_gets_403 PASSED         [ 35%]
tests/test_auth.py::test_csrf_token_in_a_plain_form_field_works PASSED   [ 35%]
tests/test_auth.py::test_account_page_is_for_members_only PASSED         [ 36%]
tests/test_auth.py::test_other_names_are_limited_to_ten PASSED           [ 36%]
tests/test_auth.py::test_full_database_gives_a_friendly_page_and_toast PASSED [ 37%]
tests/test_auth.py::test_unexpected_errors_get_a_plain_message PASSED    [ 37%]
tests/test_auth.py::test_agent_api_errors_are_503_so_the_connector_retries PASSED [ 38%]
tests/test_auth.py::test_row_level_security_is_switched_on_for_owned_tables_only PASSED [ 38%]
tests/test_auth.py::test_row_level_security_does_nothing_on_sqlite PASSED [ 39%]
tests/test_auth.py::test_pages_and_stylesheets_are_compressed_and_cached PASSED [ 39%]
tests/test_auth.py::test_downloads_are_not_compressed_again PASSED       [ 40%]
tests/test_classes.py::test_class_visibility_for_every_kind_of_viewer PASSED [ 40%]
tests/test_classes.py::test_gen_enrolment_includes_students_who_register_later PASSED [ 41%]
tests/test_classes.py::test_hidden_class_looks_like_a_missing_one PASSED [ 41%]
tests/test_classes.py::test_only_admins_create_edit_and_delete_classes PASSED [ 42%]
tests/test_classes.py::test_class_form_checks_its_fields PASSED          [ 42%]
tests/test_classes.py::test_automatic_label_when_there_is_no_title PASSED [ 43%]
tests/test_classes.py::test_class_page_shows_info_tabs_and_enrolment_summary PASSED [ 43%]
tests/test_classes.py::test_overlapping_classes_never_share_a_lane PASSED [ 44%]
tests/test_classes.py::test_three_or_more_overlapping_classes_add_lanes PASSED [ 44%]
tests/test_classes.py::test_non_overlapping_classes_reuse_lanes PASSED   [ 45%]
tests/test_classes.py::test_timeline_boxes_never_overlap_at_any_zoom PASSED [ 45%]
tests/test_classes.py::test_timeline_reaches_back_to_the_oldest_class_and_three_years_forward PASSED [ 46%]
tests/test_classes.py::test_a_tick_on_every_friday_on_its_exact_date PASSED [ 46%]
tests/test_classes.py::test_timeline_page_and_zoom PASSED                [ 47%]
tests/test_classes.py::test_enrolled_and_ended_badges_on_the_timeline PASSED [ 47%]
tests/test_classes.py::test_times_are_shown_in_jst PASSED                [ 48%]
tests/test_classes.py::test_relative_times PASSED                        [ 48%]
tests/test_classes.py::test_html_in_class_text_is_escaped PASSED         [ 49%]
tests/test_classes.py::test_classes_have_the_tabs_ticked_at_creation PASSED [ 49%]
tests/test_classes.py::test_switched_off_tabs_and_their_pages_are_not_found PASSED [ 50%]
tests/test_classes.py::test_switching_a_tab_off_hides_its_data_and_on_brings_it_back PASSED [ 50%]
tests/test_classes.py::test_the_class_form_has_the_tabs_ticked_by_default PASSED [ 50%]
tests/test_classes.py::test_existing_classes_get_all_three_tabs PASSED   [ 51%]
tests/test_classes.py::test_hidden_exam_submissions_dont_mark_the_class_as_new PASSED [ 51%]
tests/test_coursework.py::test_only_admins_post_edit_and_delete_tasks PASSED [ 52%]
tests/test_coursework.py::test_assignments_and_exams_are_kept_apart PASSED [ 52%]
tests/test_coursework.py::test_task_needs_title_due_time_and_content PASSED [ 53%]
tests/test_coursework.py::test_task_with_a_file_keeps_exactly_one_kind_of_content PASSED [ 53%]
tests/test_coursework.py::test_due_indicator PASSED                      [ 54%]
tests/test_coursework.py::test_bar_shows_the_latest_task_with_its_indicator PASSED [ 54%]
tests/test_coursework.py::test_polling_answers_204_when_nothing_changed PASSED [ 55%]
tests/test_coursework.py::test_enrolled_student_submits_and_it_appears_everywhere PASSED [ 55%]
tests/test_coursework.py::test_only_enrolled_students_can_submit PASSED  [ 56%]
tests/test_coursework.py::test_only_the_owner_edits_or_deletes_a_submission PASSED [ 56%]
tests/test_coursework.py::test_admins_can_delete_any_submission PASSED   [ 57%]
tests/test_coursework.py::test_link_only_to_a_task_of_the_same_class_and_kind PASSED [ 57%]
tests/test_coursework.py::test_submission_without_a_task_is_fine PASSED  [ 58%]
tests/test_coursework.py::test_submission_content_is_text_or_file PASSED [ 58%]
tests/test_coursework.py::test_late_badge PASSED                         [ 59%]
tests/test_coursework.py::test_dropdown_lists_five_recent_tasks_then_older PASSED [ 59%]
tests/test_coursework.py::test_file_download_headers PASSED              [ 60%]
tests/test_coursework.py::test_images_may_show_inline PASSED             [ 60%]
tests/test_coursework.py::test_files_are_kept_in_file_storage_not_the_database PASSED [ 61%]
tests/test_coursework.py::test_deleting_a_submission_deletes_its_file PASSED [ 61%]
tests/test_coursework.py::test_file_size_limit PASSED                    [ 62%]
tests/test_coursework.py::test_uploads_stop_when_storage_is_nearly_full PASSED [ 62%]
tests/test_coursework.py::test_file_permissions PASSED                   [ 63%]
tests/test_coursework.py::test_a_submission_can_have_several_files PASSED [ 63%]
tests/test_coursework.py::test_editing_removes_ticked_files_and_adds_new_ones PASSED [ 64%]
tests/test_coursework.py::test_tasks_can_have_several_files_too PASSED   [ 64%]
tests/test_coursework.py::test_one_refused_file_stores_none_of_them PASSED [ 65%]
tests/test_coursework.py::test_at_most_20_files PASSED                   [ 65%]
tests/test_coursework.py::test_old_single_files_move_to_attachments PASSED [ 66%]
tests/test_coursework.py::test_guests_cannot_download_attached_files_unless_ticked PASSED [ 66%]
tests/test_coursework.py::test_text_content_is_shown_as_plain_text PASSED [ 67%]
tests/test_coursework.py::test_deleting_a_class_deletes_its_files PASSED [ 67%]
tests/test_coursework.py::test_deleting_a_task_keeps_its_submissions PASSED [ 68%]
tests/test_coursework.py::test_due_time_is_entered_and_shown_in_jst PASSED [ 68%]
tests/test_coursework.py::test_new_submission_marks_the_class_until_its_page_is_opened PASSED [ 69%]
tests/test_coursework.py::test_new_members_only_see_marks_for_submissions_after_they_registered PASSED [ 69%]
tests/test_coursework.py::test_guests_get_no_notification_marks PASSED   [ 70%]
tests/test_coursework.py::test_supabase_keys_are_sent_the_way_each_kind_needs PASSED [ 70%]
tests/test_coursework.py::test_files_go_to_supabase_storage_and_back PASSED [ 71%]
tests/test_coursework.py::test_supabase_refusals_are_explained PASSED    [ 71%]
tests/test_coursework.py::test_storage_check_warns_about_render_without_supabase PASSED [ 72%]
tests/test_coursework.py::test_a_missing_file_gives_a_clear_message PASSED [ 72%]
tests/test_coursework.py::test_pdfs_html_and_text_can_be_previewed PASSED [ 73%]
tests/test_coursework.py::test_a_fake_pdf_is_not_previewed PASSED        [ 73%]
tests/test_coursework.py::test_previews_follow_download_permissions PASSED [ 74%]
tests/test_guests.py::test_placeholders_are_fixed_and_never_reused PASSED [ 74%]
tests/test_guests.py::test_name_search_catches_parts_case_widths_and_other_names PASSED [ 75%]
tests/test_guests.py::test_members_always_see_the_original_text PASSED   [ 75%]
tests/test_guests.py::test_guests_never_see_a_real_name_or_login_id_anywhere PASSED [ 75%]
tests/test_guests.py::test_unticked_items_do_not_exist_for_guests PASSED [ 76%]
tests/test_guests.py::test_ticked_submissions_are_shown_in_full PASSED   [ 76%]
tests/test_guests.py::test_ticked_submission_linked_to_a_hidden_task_shows_no_link PASSED [ 77%]
tests/test_guests.py::test_class_hidden_from_guests_hides_everything_in_it PASSED [ 77%]
tests/test_guests.py::test_guest_cell_shows_the_latest_guest_visible_submission PASSED [ 78%]
tests/test_guests.py::test_visible_to_guests_checkbox_warns_about_files PASSED [ 78%]
tests/test_guests.py::test_guests_are_refused_on_every_upload_edit_and_control_route PASSED [ 79%]
tests/test_guests.py::test_guests_never_see_the_members_page_or_login_ids PASSED [ 79%]
tests/test_members.py::test_members_page_groups_accounts_and_shows_storage PASSED [ 80%]
tests/test_members.py::test_admin_changes_name_and_gen PASSED            [ 80%]
tests/test_members.py::test_reset_password_shows_it_once_and_signs_the_member_out PASSED [ 81%]
tests/test_members.py::test_delete_account_needs_the_admin_password_and_removes_their_data PASSED [ 81%]
tests/test_members.py::test_admins_cannot_delete_themselves PASSED       [ 82%]
tests/test_members.py::test_add_the_next_gen PASSED                      [ 82%]
tests/test_members.py::test_folders_belong_to_their_student_and_are_shared_across_classes PASSED [ 83%]
tests/test_members.py::test_grid_pages_hold_nine_cells_including_add_agent PASSED [ 83%]
tests/test_members.py::test_members_page_shows_the_database_size PASSED  [ 84%]
tests/test_members.py::test_file_storage_breakdown_by_type_class_person_agent_and_largest PASSED [ 84%]
tests/test_members.py::test_pie_gives_six_colours_then_other PASSED      [ 85%]
tests/test_members.py::test_database_breakdown_estimates_by_class_person_and_agent PASSED [ 85%]
tests/test_members.py::test_breakdowns_are_for_admins_only PASSED        [ 86%]
tests/test_skills.py::test_a_member_shares_a_skill_and_it_appears_with_its_identifier PASSED [ 86%]
tests/test_skills.py::test_sharing_again_with_the_same_name_updates_it PASSED [ 87%]
tests/test_skills.py::test_skill_files_are_checked PASSED                [ 87%]
tests/test_skills.py::test_several_skill_files_become_several_skills PASSED [ 88%]
tests/test_skills.py::test_one_bad_skill_file_shares_none PASSED         [ 88%]
tests/test_skills.py::test_the_connector_skill_is_built_in_everywhere PASSED [ 89%]
tests/test_skills.py::test_the_connector_skill_follows_the_dashboard PASSED [ 89%]
tests/test_skills.py::test_older_databases_let_skills_have_no_class_or_owner PASSED [ 90%]
tests/test_skills.py::test_downloading_counts PASSED                     [ 90%]
tests/test_skills.py::test_class_only_and_seminar_wide_skills PASSED     [ 91%]
tests/test_skills.py::test_the_market_is_for_members_only PASSED         [ 91%]
tests/test_skills.py::test_only_its_member_edits_and_admins_can_remove PASSED [ 92%]
tests/test_skills.py::test_filters_search_source_and_mine PASSED         [ 92%]
tests/test_skills.py::test_agents_post_read_update_and_download_skills PASSED [ 93%]
tests/test_skills.py::test_agents_only_delete_their_own_skills PASSED    [ 93%]
tests/test_skills.py::test_agents_see_skills_as_their_owner_does PASSED  [ 94%]
tests/test_skills.py::test_skill_trees PASSED                            [ 94%]
tests/test_skills.py::test_a_skill_tree_needs_its_ai_version_first PASSED [ 95%]
tests/test_skills.py::test_people_see_the_user_friendly_version_first PASSED [ 95%]
tests/test_skills.py::test_user_friendly_versions_are_checked_and_protected PASSED [ 96%]
tests/test_skills.py::test_agents_set_the_user_friendly_version_after_the_ai_one PASSED [ 96%]
tests/test_skills.py::test_deleting_an_agent_deletes_its_tree_files PASSED [ 97%]
tests/test_skills.py::test_deleting_an_agent_keeps_its_skills_credited PASSED [ 97%]
tests/test_skills.py::test_keyword_rules_sort_skills_by_function PASSED  [ 98%]
tests/test_skills.py::test_the_ai_organizer_uses_one_request_and_applies_its_answer PASSED [ 98%]
tests/test_skills.py::test_a_bad_ai_answer_changes_nothing_and_the_daily_limit_holds PASSED [ 99%]
tests/test_skills.py::test_the_schedule_always_runs_at_3pm_and_once_more_only_if_needed PASSED [ 99%]
tests/test_skills.py::test_the_daily_timer_needs_its_token PASSED        [100%]
======================= 204 passed, 1 warning in 34.18s ========================
```

## 3. Manual end-to-end check (Instructions step 7)

The dev server was started with `fastapi dev main.py` with temporary registration keys and a Fernet key passed as environment variables (never written to any file in the project), a throw-away SQLite file and a throw-away file folder outside the project. A script then made real HTTP requests, and the real demo agent (`agent/agent.py`) was run against the server. Every process was stopped afterwards.

```text
PASS  register admin sensei
PASS  register student taro
PASS  register student hanako
PASS  log in as sensei
PASS  log in as taro
PASS  log in as hanako
PASS  admin creates a class enrolling Gen 9
PASS  admin posts an assignment
PASS  student submits a file linked to the assignment
PASS  submission appears under the assignment on the history page
PASS  submission appears in the student's cell
PASS  a member can download the file
PASS  a student hidden nowhere but not enrolled still sees the class
PASS  guest sees the class
PASS  guest sees the (ticked) assignment
PASS  guest can't get the unticked submission's file
PASS  guest never sees the student's name
PASS  student adds an agent
PASS  demo agent appears online in the Agents tab
PASS  student sends Start
PASS  the Start command was picked up by the agent
PASS  the agent's outputs arrived
PASS  a message reached the agent
PASS  demo agent stopped
```

Result: 24 of 24 checks passed. The pages were also looked at in a browser (timeline centred on today, class page with the bar and gen-framed student window, the task pop-up, a read-only agent page, and the class page at phone width without sideways scrolling).

## 4. Known limitations and what could not be verified

- **Render and Supabase were not used.** Deploying (Render Blueprint, Supabase Postgres via the Session pooler, the private `seminar-files` bucket) can only be done by the user. The Supabase Storage calls in `storage.py` (upload `POST /storage/v1/object/{bucket}/{path}`, download `GET` on the same path, delete `DELETE /storage/v1/object/{bucket}` with `prefixes`) follow Supabase's Storage REST API but were never run against a real project. The tests check them against a stand-in server that follows Supabase's documented API (including sending `sb_secret_` keys only in the `apikey` header); whether the real service agrees can only be seen on the deployed site, where the Members page now shows a live connection check. Postgres was likewise not tested (only SQLite); `add_missing_columns()` was tested on SQLite.
- **The database breakdown "By table" was only exercised through its fallback**: this Mac's SQLite can't measure tables, so the exact per-table sizes (Postgres's `pg_total_relation_size`) will first be seen on Supabase. The pie charts were checked by eye in one browser.
- **Not tested against the real services:** the AI organizer was never run against OpenRouter (on purpose: the free key allows very few requests a day); its request and reply handling are tested with a stand-in. Row level security was tested with a stand-in Postgres connection only; Supabase's own Security advisor will confirm it after deploying. The GitHub timer can only run once the repository has its two secrets.
- **Browser-only behaviour is not covered by the automated tests**: drag-to-pan, dropping files on upload fields (checked in the browser with simulated drops, not with real files dragged from the desktop), centring the timeline, zoom keeping the centre date, the background grid's rush, the typing title and other animations, and HTMX/Alpine interactions. They were checked by eye in one desktop browser and at phone width, not across browsers or on a real touch device.
- **Rate limiting is in memory**, per address taken from the first `X-Forwarded-For` entry. It resets when the site restarts, and is only correct while the site runs as a single instance (as on Render's free plan). A client can put a fake first address in `X-Forwarded-For`, so the limit slows guessing rather than making it impossible.
- **The name search cannot read inside files**, and it only knows names written as full name, name parts of 2+ characters, or listed "other names". A name written some other way (for example an unlisted spelling, or half-width katakana with separate voicing marks) is not caught.
- **Changing a student's gen gives them a new placeholder number in the new gen**, so guests following "Gen 9 Student 3" would see that student under a new placeholder after the move.
- **Inbox uploads are refused until the agent opts in** with `accept_inbox()` (the prompt's tests ask for the Inbox to be refused until then; the earlier project allowed uploads that waited).
- **Notification bells are stored but nothing sends notifications yet** (a future Discord bot).
- The deprecation warning printed by pytest (`Using httpx with starlette.testclient is deprecated`) comes from the installed Starlette version and does not affect the results.
