import streamlit as st

st.set_page_config(
    page_title="Dancing with the SARs",
    page_icon="🛰️",
    layout="wide",
)

st.title("Dancing with the SARs")
st.subheader("Earth Surface Change Observatory")

st.markdown("""
Explore how Synthetic Aperture Radar observations
can reveal changes occurring across Earth's surface.

Our mission is to transform satellite observations
into understandable environmental and humanitarian insights.
""")

st.info(
    "Development stage: Data acquisition and processing "
    "are being prepared. No live satellite analysis is "
    "available yet."
)

st.divider()

col1, col2, col3 = st.columns(3)

col1.metric("Data sources", "In development")
col2.metric("Observations", "Not loaded")
col3.metric("Change analysis", "Not available")

st.markdown("### Project modules")

st.markdown("""
- **Data Explorer:** Discover available SAR observations.
- **Change Detection:** Compare satellite observations.
- **Impact Assessment:** Explore potential environmental
  and humanitarian implications.
""")

st.caption("NASA Space Apps Challenge | Dancing with the SARs")
