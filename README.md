## Steamrise - Apprise Notifications for Steam games price changes

### How to use: - 

1. Download the ```compose.yml``` and ```.env``` files from the repo [here](https://github.com/driftywinds/steamrise).
2. Add your Telegram Bot token to the ```.env``` file.
3. Run ```docker compose up -d```.
4. You can see the endpoints Apprise supports and their config URLs [here](https://github.com/caronc/apprise?tab=readme-ov-file#supported-notifications)) to use notifications from this bot outside of Telergam.

<br>

You can check logs live with this command: - 
```
docker compose logs -f
```
### For dev testing: -
- have python3 installed on your machine
- clone the repo
- go into the directory and run these commands: -
```
python3 -m venv .venv
source .venv/bin/activate
pip install --no-cache-dir -r requirements.txt
```  
- configure ```.env``` variables.
- then run ```python3 head.py```
