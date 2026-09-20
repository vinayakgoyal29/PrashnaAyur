import re

with open("kiosk_app.py", "r") as f:
    lines = f.readlines()

new_lines = []
i = 0
while i < len(lines):
    line = lines[i]
    
    # 1. Document Scanner container
    if "with st.expander(S(\"scanner_heading\"), expanded=False):" in line:
        new_lines.append('    with st.container(border=True):\n')
        # indent until we hit st.divider()
        while i < len(lines) and not "st.divider()" in lines[i]:
            new_lines.append("    " + lines[i] if lines[i].strip() else lines[i])
            i += 1
        continue
        
    # 2. Triage Interview container
    if "st.subheader(S(\"triage_heading\"))" in line:
        new_lines.append('    with st.container(border=True):\n')
        # indent until the end of IN_TRIAGE (which is before STAGE: COMPLETE)
        while i < len(lines) and not "elif stage == \"COMPLETE\":" in lines[i]:
            # Inject CSS and column layout before mic_recorder
            if "audio_data = mic_recorder(" in lines[i]:
                # inject CSS
                css = '''
                st.html("""
                <style>
                iframe[title="streamlit_mic_recorder.mic_recorder"] {
                    width: 120px !important;
                    height: 120px !important;
                    border-radius: 50% !important;
                    background-color: #005C97 !important;
                    border: none !important;
                    display: block !important;
                    margin: 0 auto !important;
                }
                div[data-testid="stHorizontalBlock"] button {
                    width: 120px !important;
                    height: 120px !important;
                    border-radius: 50% !important;
                    background-color: #005C97 !important;
                    color: white !important;
                    border: none !important;
                    font-weight: bold !important;
                    display: flex !important;
                    justify-content: center !important;
                    align-items: center !important;
                    margin: 0 auto !important;
                }
                </style>
                """)
                col1, col2, col3 = st.columns([1, 1, 1])
                with col2:
'''
                for css_line in css.strip('\n').split('\n'):
                    new_lines.append("        " + css_line + "\n")
                
                # Now indent the mic_recorder block + 4 spaces
                # We need to indent all lines until the `if audio_data and audio_data.get("bytes"):`
                while i < len(lines) and not 'if audio_data and audio_data.get("bytes"):' in lines[i]:
                    new_lines.append("        " + lines[i] if lines[i].strip() else lines[i])
                    i += 1
                
                # Now append the rest inside the container (indented)
                if i < len(lines) and not "elif stage == \"COMPLETE\":" in lines[i]:
                    # The `if audio_data...` block should be at the same level as `col1, col2, col3` meaning indented by 4 spaces (total 8 spaces from `with st.container()`)
                    pass # Handled by the generic indenter below
            
            if i < len(lines) and not "elif stage == \"COMPLETE\":" in lines[i]:
                new_lines.append("    " + lines[i] if lines[i].strip() else lines[i])
                i += 1
        continue
    
    new_lines.append(line)
    i += 1

with open("kiosk_app.py", "w") as f:
    f.writelines(new_lines)

