import requests

def get_kraken_assets():
    response = requests.get("https://api.kraken.com/0/public/Assets")
    data = response.json()
    assets = data.get('result', {})
    for asset_name, asset_info in assets.items():
        if asset_name == 'ZUSD':
            print(f"Asset Name: {asset_name}, Altname: {asset_info.get('altname')}")
        if asset_name == 'USDT':
            print(f"Asset Name: {asset_name}, Altname: {asset_info.get('altname')}")

get_kraken_assets()
