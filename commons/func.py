import time


def getCurrentTimeString(no_space=True):
    t = time.localtime()
    currentTime = time.strftime("%Y-%m-%d %H:%M:%S", t)
    if no_space:
        currentTime = currentTime.replace(" ", "_")
    return currentTime


def list_div(ls, v):
    re = []
    for s in ls:
        re.append(s / v)
    return s