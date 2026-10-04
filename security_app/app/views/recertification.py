import streamlit as st

from cdp_security import queries, service, ui

user = ui.user()
st.title("Access reviews")
ui.show_flash()
st.caption("Confirm who still needs access. Anything not reviewed by the due date is listed for removal.")

items = queries.recert_items_for(user.email)
if not items:
    st.info("You have no access reviews due.")
    st.stop()

rows = [{"item_id": i["item_id"], "Person": i["user_email"], "Access": f"{i['data_product_name']} ({i['group_name']})",
         "Classification": i["classification_code"], "Ends": i["valid_to"].date(),
         "Last reviewed": i["last_recertified_at"].date() if i["last_recertified_at"] else None,
         "Due": i["due_at"].date(), "Decision": "Not reviewed", "Comment": ""} for i in items]
edited = st.data_editor(
    rows, hide_index=True, use_container_width=True, disabled=["item_id", "Person", "Access", "Classification",
                                                              "Ends", "Last reviewed", "Due"],
    column_config={"item_id": None,
                   "Decision": st.column_config.SelectboxColumn(options=["Not reviewed", "Keep", "Remove"],
                                                                required=True)})
if st.button("Save decisions", type="primary"):
    done, errors = 0, []
    for row in edited:
        if row["Decision"] == "Not reviewed":
            continue
        try:
            service.recert_decide(user, row["item_id"], row["Decision"] == "Keep", row["Comment"])
            done += 1
        except service.ServiceError as exc:
            errors.append(f"{row['Person']} / {row['Access']}: {exc}")
    for e in errors:
        st.error(e)
    if done and not errors:
        ui.flash(f"{done} decision(s) saved.")
        st.rerun()
