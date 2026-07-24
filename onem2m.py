#!/usr/bin/env python3
"""
onem2m.py -- tiny oneM2M HTTP client, enough for this project.

Talks to an ACME CSE over plain HTTP using standard oneM2M headers, so the same
code works against other oneM2M CSEs (e.g. Eclipse OM2M) with only the base URL
changed. No vendor library -- you can see exactly what's on the wire.

oneM2M essentials, in one breath:
  - Everything is a resource in a tree under the CSE base (default CSE-ID 'cse-in',
    resource name 'cse-in' -> URL .../cse-in).
  - An AE (Application Entity) is a registered app. Create once; it returns an
    'aei' (AE-ID) you pass as the originator on later requests.
  - A container (cnt) is a folder. A contentInstance (cin) is one immutable
    reading dropped into a container. You POST readings; consumers GET the latest
    or subscribe.
  - Resource types travel as a numeric 'ty' query param: AE=2, container=3,
    contentInstance=4, subscription=23.
  - The originator header 'X-M2M-Origin' identifies who is asking. For first-time
    AE creation you may use a provisional originator like 'CAdmin' (ACME default
    admin, note the capital A) or a chosen 'C...' id; afterwards use the returned aei.
"""

import json
import urllib.request
import urllib.error

TY = {"ae": 2, "cnt": 3, "cin": 4, "sub": 23}


class OneM2M:
    def __init__(self, base, cse_name="cse-in", originator="CAdmin", rvi="3"):
        # base like "http://localhost:8080"
        self.root = f"{base.rstrip('/')}/{cse_name}"
        self.originator = originator
        self.rvi = rvi
        self._rqi = 0

    def _headers(self, ty=None, originator=None):
        self._rqi += 1
        h = {
            "X-M2M-Origin": originator or self.originator,
            "X-M2M-RI": f"rqi{self._rqi}",
            "X-M2M-RVI": self.rvi,
            "Accept": "application/json",
        }
        # oneM2M encodes the resource type in the Content-Type on create
        h["Content-Type"] = "application/json" + (f";ty={ty}" if ty else "")
        return h

    def _req(self, method, url, ty=None, body=None, originator=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method,
                                     headers=self._headers(ty, originator))
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                raw = r.read().decode()
                return r.status, (json.loads(raw) if raw else {})
        except urllib.error.HTTPError as e:
            raw = e.read().decode(errors="ignore")
            return e.code, raw

    # ---- resource creation (idempotent-ish: 409 = already exists, treated ok) ----

    def ensure_ae(self, rn, app_id="Nwifi-csi"):
        """Register an AE and return its AE-ID (aei).

        A new AE must register with a provisional originator (a 'C...' id it
        proposes), NOT the admin id -- the CSE assigns/confirms the aei. If the AE
        already exists we GET it (using that same originator) to recover the aei.
        """
        prov = "C" + rn.replace("-", "")          # e.g. 'Cwifisensing'
        body = {"m2m:ae": {"rn": rn, "api": app_id, "rr": True, "srv": [self.rvi]}}
        st, resp = self._req("POST", self.root, TY["ae"], body, originator=prov)
        if st in (200, 201):
            return resp["m2m:ae"]["aei"]
        if st == 409:                              # already registered -> recover aei
            st2, resp2 = self._req("GET", f"{self.root}/{rn}", originator=prov)
            if st2 == 200:
                return resp2["m2m:ae"]["aei"]
            st3, resp3 = self._req("GET", f"{self.root}/{rn}", originator=self.originator)
            if st3 == 200:
                return resp3["m2m:ae"]["aei"]
            raise RuntimeError(f"AE '{rn}' exists but GET failed: {st2} {resp2}")
        raise RuntimeError(f"AE '{rn}' registration failed: {st} {resp}")

    def ensure_container(self, parent_path, rn, mni=60, originator=None):
        body = {"m2m:cnt": {"rn": rn, "mni": mni}}
        st, resp = self._req("POST", f"{self.root}/{parent_path}", TY["cnt"], body,
                             originator=originator)
        if st in (200, 201, 409):
            return f"{parent_path}/{rn}"
        raise RuntimeError(f"container '{rn}' failed: {st} {resp}")

    def post_cin(self, container_path, content, originator=None):
        """Drop one contentInstance. `content` may be str/number/dict (ACME accepts JSON)."""
        body = {"m2m:cin": {"cnf": "application/json:0", "con": content}}
        st, resp = self._req("POST", f"{self.root}/{container_path}", TY["cin"], body,
                             originator=originator)
        if st not in (200, 201):
            raise RuntimeError(f"cin post failed: {st} {resp}")
        return resp

    def latest_cin(self, container_path, originator=None):
        st, resp = self._req("GET", f"{self.root}/{container_path}/la",
                             originator=originator)
        if st == 200:
            return resp["m2m:cin"]["con"]
        return None

    def subscribe(self, container_path, notify_url, rn="sub", originator=None):
        """Create a subscription so the CSE POSTs notifications to notify_url on
        each new child (nct=1 -> notify with the resource; enc/net=[3] -> on
        create-of-child)."""
        body = {"m2m:sub": {"rn": rn, "nu": [notify_url],
                            "enc": {"net": [3]}, "nct": 1}}
        st, resp = self._req("POST", f"{self.root}/{container_path}", TY["sub"], body,
                             originator=originator)
        if st in (200, 201, 409):
            return True
        raise RuntimeError(f"subscription failed: {st} {resp}")