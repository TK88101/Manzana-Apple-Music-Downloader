import re
import json
import requests

from bs4 import BeautifulSoup
from urllib.parse import urlparse
from base64 import b64decode, b64encode
from rich.console import Console

from core import parse
from utils import logger
from utils import cache
from utils import keys
from utils import config
from utils import Widevine
from utils import WidevinePsshData

from . import artist
from . import album
from . import musicvideo

cons = Console()

class AppleMusic:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers = {
            'connection': 'keep-alive',
            'accept': 'application/json',
            'origin': 'https://music.apple.com',
            'referer': 'https://music.apple.com/',
            'accept-encoding': 'gzip, deflate, br',
            'content-type': 'application/json;charset=utf-8',
            'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/110.0.0.0 Safari/537.36'
        }

        self.__check_access_token()
        self.__check_media_user_token()

    def __parse_url(self, url):
        logger.debug("Parsing url...")

        self.songId = None
        u = urlparse(url)

        if not u.scheme:
            url = f"https://{url}"

        if u.netloc == "music.apple.com":
            s = u.path.split('/')
            self.kind = s[2]
            self.id = s[-1]
            if u.query:
                self.songId = u.query.replace('i=', '')

            logger.debug(f'UrlParseResult(kind="{self.kind}", id="{self.id}", songId="{self.songId}")')
        else:
            logger.error("Url is invalid!", 1)

    def __check_access_token(self):
        def get_access_token():
            logger.info("Fetching accessToken from web...")
            r = requests.get('https://music.apple.com/us/browse')
            c = BeautifulSoup(r.text, "html.parser")
            js = c.find(
                "script",
                attrs={
                    'type': 'module',
                    'crossorigin': True,
                    'src': True
                }
            ).get('src')
            r = requests.get(f'https://music.apple.com{js}')
            at = re.search('(?=eyJh)(.*?)(?=")', r.text).group(1)
            logger.debug(f'access-token: {at}')
            return at
        
        at = config.get('accessToken')
        if at:
            logger.info("Checking accessToken...")
            self.session.headers['authorization'] = f'Bearer {at}'
            r = self.session.get("https://amp-api.music.apple.com/v1/catalog/us/songs/1450330685")
            if r.text == "":
                logger.error("accessToken is expired!")
                at = get_access_token()
                self.session.headers['authorization'] = f'Bearer {at}'
                config.set('accessToken', at)
            else: logger.debug(f'accessToken is working! access-token: {at}')
        else:
            at = get_access_token()
            self.session.headers['authorization'] = f'Bearer {at}'
            config.set('accessToken', at)

    def __check_media_user_token(self):
        mut = config.get('mediaUserToken')
        logger.info("Checking mediaUserToken...")
        self.session.headers['media-user-token'] = mut
        r = self.session.get("https://amp-api.music.apple.com/v1/me/storefront")

        if r.status_code == 200:
            r = json.loads(r.text)
            self.storefront = r["data"][0]["id"]
            self.language = r["data"][0]["attributes"]["defaultLanguageTag"]
            logger.debug(f"mediaUserToken is working! mediaUserToken: {mut}")
            self.session.headers['accept-language'] = f'{self.language},en;q=0.9'
        else:
            logger.error("Your mediaUserToken is invalid! Enter again to continue...")
            config.delete('mediaUserToken')
            mut = input("\n\tmediaUserToken: "); print()
            config.set('mediaUserToken', mut)
            self.__check_media_user_token()

    def __get_artist(self, url):
        def __get_res(session, apiUrl):
            urls = []

            while True:
                r = session.get(apiUrl)
                r = json.loads(r.text)

                for item in r["data"]:
                    name = item["attributes"]["name"]
                    if " - EP" in name: name = name.replace(" - EP", "") + " [EP]"
                    if " - Single" in name: name = name.replace(" - Single", "") + " [S]"
                    if item["attributes"].get("contentRating"): name += " [E]"

                    urls.append(
                        {
                            "url": item["attributes"].get("url"),
                            "name": name,
                            "contentId": item["id"]
                        }
                    )

                if "next" in r:
                    nextUrl = r["next"]
                    apiUrl = f'https://amp-api.music.apple.com/{nextUrl}'
                else:
                    break

            return urls

        r = requests.get(url)
        c = BeautifulSoup(r.text, "html.parser")

        name = c.find(
            "meta",
            attrs={
                'name': 'apple:title',
                'content': True
            }
        ).get('content')

        id = c.find(
            "meta",
            attrs={
                'name': 'apple:content_id',
                'content': True
            }
        ).get('content')

        logger.info(f"Fetching artist contents...")
        cons.print(f'\n\t [dim]Artist:[/] {name}')
        
        return artist.get_urls(
            __get_res(
                self.session,
                f"https://amp-api.music.apple.com/v1/catalog/{self.storefront}/artists/{id}/view/full-albums?limit=100"
            ),
            __get_res(
                self.session,
                f"https://amp-api.music.apple.com/v1/catalog/{self.storefront}/artists/{id}/view/singles?limit=100"
            ),
            __get_res(
                self.session,
                f"https://amp-api.music.apple.com/v1/catalog/{self.storefront}/artists/{id}/view/music-videos?limit=100"
            ),
            name
        )

    def __get_api(self):
        logger.info("Fetching API response...")
        params = None

        if self.kind == "album":
            params = {
                'extend': 'editorialVideo',
                'include[songs]': 'lyrics,credits'
            }

        r = self.session.get(
            f"https://amp-api.music.apple.com/v1/catalog/{self.storefront}/{self.kind}s/{self.id}?l={self.language}",
            params=params
        )

        if r.status_code != 200:
            logger.error(f"[{r.status_code}] {r.reason}: {r.content}", 1)
        r = json.loads(r.text)

        if not "errors" in r:
            return r
        else:
            errors = r["errors"]
            if not isinstance(errors, list): errors = [errors]
            for error in errors:
                logger.error(
                    "{err_status}: {err_detail}".format(
                        err_status=error.get("status"),
                        err_details=error.get("detail")
                    )
                )
            exit(1)

    def __get_info(self):
        data = cache.get(self.id)
        if data:
            logger.info("Using data found in cache...")
            return data
        
        if self.kind == "album":
            data = album.parse_data(
                self.__get_api()["data"][0]
            )
        elif self.kind == "music-video":
            data = musicvideo.parse_data(
                self.__get_api()["data"][0]
            )
        
        cache.set(self.id, data)
        return data
    
    def __get_webplayback(self, id):
        logger.info("Getting webplayback...")

        r = self.session.post(
            url="https://play.itunes.apple.com/WebObjects/MZPlay.woa/wa/webPlayback",
            data=json.dumps({'salableAdamId': id})
        )

        if r.status_code != 200:
            logger.error(f"[{r.status_code}] {r.reason}: {r.content}", 1)
        r = json.loads(r.text)

        if not "failureType" in r:
            return r["songList"][0]
        else:
            er = r.get("customerMessage")
            if er: logger.error(er)
            else: logger.error("Unable to get webplayback!")

    def __get_license(self, assetId, keyUri, challenge="CAQ="):
        r = self.session.post(
            url=self.licenseUrl,
            data=json.dumps(
                {
                    "adamId": assetId,
                    "challenge": challenge,
                    "isLibrary": False,
                    "key-system": "com.widevine.alpha",
                    "uri": keyUri,
                    "user-initiated": True
                }
            )
        )

        if r.status_code != 200:
            logger.error(f"[{r.status_code}] {r.reason}: {r.content}", 1)
        
        r = json.loads(r.text)
        if not "license" in r:
            logger.error("Unable to get license!", 1)
        return r.get("license")
    
    def __get_song_keys(self, songId, keyUri):
        cert_data_b64 = self.__get_license(songId, keyUri)

        dataPSSH = WidevinePsshData()
        dataPSSH.algorithm = 1
        dataPSSH.key_id.append(b64decode(keyUri.split(",")[1]))

        pssh = b64encode(dataPSSH.SerializeToString()).decode("utf8")

        widevine = Widevine(
            init_data=pssh,
            cert_data=cert_data_b64,
            device_name=config.get('deviceName'),
            device_path=config.get('devicePath')
        )

        license = self.__get_license(
            songId, keyUri,
            b64encode(
                widevine.get_challenge()
            ).decode("utf-8")
        )
        
        widevine.update_license(license)
        return widevine.get_keys()

    def __get_musicvideo_keys(self, musicVideoId, keyUri):
        cert_data_b64 = self.__get_license(musicVideoId, keyUri)
        
        widevine = Widevine(
            init_data=keyUri.split(",")[-1],
            cert_data=cert_data_b64,
            device_name=config.get('deviceName'),
            device_path=config.get('devicePath')
        )

        license = self.__get_license(
            musicVideoId, keyUri,
            b64encode(
                widevine.get_challenge()
            ).decode("utf-8")
        )

        widevine.update_license(license)
        return widevine.get_keys()
    
    def get_urls(self, urls: list):
        u = []
        for url in urls:
            if "/artist/" in url:
                self.__parse_url(url)
                for ul in self.__get_artist(url):
                    u.append(ul)
            else: u.append(url)
        return u

    def get_info(self, url):
        self.__parse_url(url)
        return [self.__get_info()]

    def get_content(self, data):
        id = data.get("id")

        wp = self.__get_webplayback(id)
        if not wp: wp = {}

        self.licenseUrl = wp.get("hls-key-server-url")

        if "hls-playlist-url" in wp:
            assetUrl = wp["hls-playlist-url"]
            logger.info("Parsing music-video uri...")
            streams = parse.mv_uri(assetUrl)
            psshs = parse.mv_psshs(streams)

            for pssh in psshs:
                logger.info("Checking decrypt keys...")
                dec_key = keys.get(pssh)

                if not dec_key:
                    logger.info("Requesting decrypt keys...")
                    dec_key = self.__get_musicvideo_keys(id, f'data:text/plain;base64,{pssh}')
                    if dec_key:
                        logger.info("Saving decrypt keys...")
                        keys.set(pssh, dec_key)
                    else: logger.warning("Unable to get decrypt keys!")
                else: logger.info("Using decrypt keys found in vault...")

            data["streams"] = streams

        elif "assets" in wp:
            asset = next((asset for asset in wp["assets"] if asset["flavor"] == "28:ctrp256"), None)
            if not asset: logger.error("Failed to find 28:ctrp256 asset!")

            assetUrl = asset.get("URL")
            metadata = asset.get("metadata")
            
            if "discCount" in metadata:
                data["disccount"] = metadata.get("discCount")

            logger.info("Parsing song uri...")
            stream = parse.aud_uri(assetUrl)

            pssh = stream["pssh"]
            logger.info("Checking decrypt keys...")
            key = keys.get(pssh)

            if not key:
                logger.info("Requesting decrypt keys...")
                key = self.__get_song_keys(id, f'data:;base64,{pssh}')
                logger.info("Saving decrypt keys...")
                keys.set(pssh, key)
            else: logger.info("Using decrypt key found in vault...")

            data["streams"] = stream

        else:
            data["streams"] = False
            return False
        return True