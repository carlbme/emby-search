import uvicorn

uvicorn.run("portal.main:app", host="0.0.0.0", port=8000, server_header=False,
            proxy_headers=False, access_log=False, workers=1)
