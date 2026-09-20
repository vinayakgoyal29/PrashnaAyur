import streamlit as st
from streamlit_mic_recorder import mic_recorder
st.html("<style>.my-mic { border: 1px solid red; }</style>")
audio_data = mic_recorder(
    start_prompt="Tap to Speak",
    stop_prompt="Recording -- Tap to Stop",
    use_container_width=True,
    format="webm",
    key="mic_recorder",
)
