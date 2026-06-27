import json
import sys
import traceback
from datetime import datetime
from logger import logger, HandleCritical, HandleException

def FileWrite(file:str, mode:str, content:str):
    with open(file, mode, encoding='utf-8') as file:
        file.write(content)

def FileRead(file:str):
    with open(file, 'r',encoding='utf-8') as file:
        content = file.read().splitlines()
    return content

def JsonWrite(file, content):
    with open(file, 'w', encoding='utf-8') as file:
        json.dump(content, file, ensure_ascii=False, indent=2)

def JsonRead(file):
    with open(file, 'r',encoding='utf-8') as file:
        content = file.read()
    return json.loads(content)




import aiohttp

async def async_fetch(session: aiohttp.ClientSession, url: str, params: dict = None, headers: dict = None, cookies: dict = None, timeout: int = 10):
    try:
        logger.debug(f'Requesting {url}...')
        async with session.get(
            url, 
            params=params, 
            headers=headers, 
            cookies=cookies, 
            timeout=aiohttp.ClientTimeout(total=timeout)
        ) as response:
            response.raise_for_status()
            
            logger.debug(f'Status: {response.status}. Request completed')
            
            data = await response.json()
            headers = json.dumps(dict(response.headers))
            
            

            return data, headers
        
    except aiohttp.ClientResponseError as e:
        logger.error(f'{e.status} {e.message} at url {response.url}')
    except TimeoutError as e:
        logger.error(f"Timeout ({timeout}s): {e} at url {url} ")
    except Exception as e: logger.error(f'Unexpected error occurred: {e}'); return None, None