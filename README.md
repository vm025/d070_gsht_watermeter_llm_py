This project has the following parts:

a) camera functionality for the android phone's browser. It captures the water meter's photo
& sends it to the LLM to extract. Traditional OCR libs did not work reliably.
 So, had to bring in the LLM.

b) write data in the required format:  the data gets written on the google sheet file.

c) graph functionality: can be viewed on phone / desktop.



Streamlit fallback-checks standard OS environment variables if it can't find a 
local secrets.toml file.

=============================================================================

1) installation:  $  uv add streamlit google-genai Pillow tzdata  ...


2) to run locally:   
  camera:  uv run streamlit run ./src/d070_gsht_watermeter_llm_py/meter.py

  graph:   uv run streamlit run ./src/d058_watermeter_llm_py/graph.py

    ====================