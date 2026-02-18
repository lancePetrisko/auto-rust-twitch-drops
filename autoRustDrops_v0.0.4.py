import requests
from bs4 import BeautifulSoup
import webbrowser
import time

#  testdasd
# Grabs all streamer links
def get_streamer_links():
    url = 'https://twitch.facepunch.com/#get-started'
    response = requests.get(url)
    soup = BeautifulSoup(response.text, 'html.parser')

    streamer_cards = soup.select(".drop-box")

    streamers = []
    for card in streamer_cards:
        link_tag = card.select_one(".drop-box-header a.streamer-info")
        name_tag = card.select_one(".streamer-name")
        if not (link_tag and name_tag):
            continue

        name = name_tag.text.strip()
        link = link_tag['href']
        is_live = "ONLINE" if "is-live" in card.get("class", []) else "OFFLINE"

        streamers.append((name, link, is_live))

    return streamers

# Opens Streamers Streams
# Makes sure its not already completed
def open_first_online_streamer(streamers, completed_streamers):
    for name, link, status in streamers:
        if status == "ONLINE" and name not in completed_streamers:
            print(f"\n Opening {name}'s stream, still not completed: {link}")
            webbrowser.open(link)
            return
    print("\n No eligible streamers are currently online.")

# Check if some drops are completed
def get_completed_streamers():
    url = 'https://www.twitch.tv/drops/inventory'
    response = requests.get(url)
    soup = BeautifulSoup(response.text, 'html.parser')

    completed = set()
    in_progress = {}  # {streamer: ("45%", minutes_remaining)}

    default_minutes = 120  # 2 hours total

    status_tags = soup.select(".ScCardDropStatusText")
    for tag in status_tags:
        parent = tag.find_parent("div", class_="ScCardDropCard")
        if not parent:
            continue
        streamer_tag = parent.select_one(".ScCardDropCampaignTitleText")
        if not streamer_tag:
            continue

        name = streamer_tag.text.strip()
        title = tag.get("title", "").strip()

        if title == "Completed":
            completed.add(name)
        elif "%" in title:
            percent = int(title.split("%")[0])
            remaining = default_minutes * (100 - percent) / 100
            in_progress[name] = (f"{percent}%", int(remaining))

    return completed, in_progress

# Waiting for completion
def wait_for_drop_completion(name, minutes):
    print(f"\n Watching {name} for {minutes} more minutes...")
    for i in range(minutes):
        print(f"  > {i+1}/{minutes} minutes passed...")
        time.sleep(60)  # wait 1 minute


# Main loop with command interface
def main():
    print("Twitch Drops Watcher is running! Type 'open' to start or 'exit' to quit.\n")

    while True:
        streamers = get_streamer_links()
        completed, in_progress = get_completed_streamers()

        for name, link, status in streamers:
            icon = "[ONLINE]" if status == "ONLINE" else "[OFFLINE]"
            if name in completed:
                extra = "(Completed)"
            elif name in in_progress:
                extra = f"({in_progress[name][0]} - est {in_progress[name][1]} min left)"
            else:
                extra = ""
            print(f"{icon} {name}: {link} {extra}")

        cmd = input("\n>> Type 'open' to auto-watch or 'exit' to quit: ").strip().lower()
        if cmd == "exit":
            print("Exiting Twitch Drops Watcher.")
            break
        elif cmd == "open":
            while True:
                streamers = get_streamer_links()
                completed, in_progress = get_completed_streamers()

                # Find a valid stream
                for name, link, status in streamers:
                    if status == "ONLINE" and name not in completed:
                        print(f"\n Opening {name}'s stream: {link}")
                        webbrowser.open(link)

                        if name in in_progress:
                            wait_for_drop_completion(name, in_progress[name][1])
                        else:
                            print("No progress info. Waiting 15 minutes...")
                            time.sleep(900)
                        print("\n Rechecking drop completion...")
                        break  # after watching one, go back to top of 'open' loop
                else:
                    # No one online or eligible — wait and retry
                    print("\n⏳ No eligible streamers online. Refreshing in 30 minutes...")
                    time.sleep(1800)
                    continue  # retry entire loop
                break  # found and watched a streamer; break inner loop
        else:
            print("Unknown command. Try 'open' or 'exit'.")
        print()


if __name__ == "__main__":
    main()

import requests
from bs4 import BeautifulSoup
import webbrowser
import time

# Grabs all streamer links
def get_streamer_links():
    url = 'https://twitch.facepunch.com/#get-started'
    response = requests.get(url)
    soup = BeautifulSoup(response.text, 'html.parser')

    streamer_cards = soup.select(".drop-box")

    streamers = []
    for card in streamer_cards:
        link_tag = card.select_one(".drop-box-header a.streamer-info")
        name_tag = card.select_one(".streamer-name")
        if not (link_tag and name_tag):
            continue

        name = name_tag.text.strip()
        link = link_tag['href']
        is_live = "ONLINE" if "is-live" in card.get("class", []) else "OFFLINE"

        streamers.append((name, link, is_live))

    return streamers

# Opens Streamers Streams
# Makes sure its not already completed
def open_first_online_streamer(streamers, completed_streamers):
    for name, link, status in streamers:
        if status == "ONLINE" and name not in completed_streamers:
            print(f"\n Opening {name}'s stream, still not completed: {link}")
            webbrowser.open(link)
            return
    print("\n No eligible streamers are currently online.")

# Check if some drops are completed
def get_completed_streamers():
    url = 'https://www.twitch.tv/drops/inventory'
    response = requests.get(url)
    soup = BeautifulSoup(response.text, 'html.parser')

    completed = set()
    in_progress = {}  # {streamer: ("45%", minutes_remaining)}

    default_minutes = 120  # 2 hours total

    status_tags = soup.select(".ScCardDropStatusText")
    for tag in status_tags:
        parent = tag.find_parent("div", class_="ScCardDropCard")
        if not parent:
            continue
        streamer_tag = parent.select_one(".ScCardDropCampaignTitleText")
        if not streamer_tag:
            continue

        name = streamer_tag.text.strip()
        title = tag.get("title", "").strip()

        if title == "Completed":
            completed.add(name)
        elif "%" in title:
            percent = int(title.split("%")[0])
            remaining = default_minutes * (100 - percent) / 100
            in_progress[name] = (f"{percent}%", int(remaining))

    return completed, in_progress

# Waiting for completion
def wait_for_drop_completion(name, minutes):
    print(f"\n Watching {name} for {minutes} more minutes...")
    for i in range(minutes):
        print(f"  > {i+1}/{minutes} minutes passed...")
        time.sleep(60)  # wait 1 minute


# Main loop with command interface
def main():
    print("Twitch Drops Watcher is running! Type 'open' to start or 'exit' to quit.\n")

    while True:
        streamers = get_streamer_links()
        completed, in_progress = get_completed_streamers()

        for name, link, status in streamers:
            icon = "[ONLINE]" if status == "ONLINE" else "[OFFLINE]"
            if name in completed:
                extra = "(Completed!)"
            elif name in in_progress:
                extra = f"({in_progress[name][0]} - est {in_progress[name][1]} min left)"
            else:
                extra = ""
            print(f"{icon} {name}: {link} {extra}")

        cmd = input("\n>> Type 'open' to auto-watch or 'exit' to quit: ").strip().lower()
        if cmd == "exit":
            print("Exiting Twitch Drops Watcher.")
            break
        elif cmd == "open":
            while True:
                streamers = get_streamer_links()
                completed, in_progress = get_completed_streamers()

                # Find a valid stream
                for name, link, status in streamers:
                    if status == "ONLINE" and name not in completed:
                        print(f"\n Opening {name}'s stream: {link}")
                        webbrowser.open(link)

                        if name in in_progress:
                            wait_for_drop_completion(name, in_progress[name][1])
                        else:
                            print("No progress info. Waiting 15 minutes...")
                            time.sleep(900)
                        print("\n Rechecking drop completion...")
                        break  # after watching one, go back to top of 'open' loop
                else:
                    # No one online or eligible — wait and retry
                    print("\n No eligible streamers online. Refreshing in 30 minutes...")
                    time.sleep(1800)
                    continue  # retry entire loop
                break  # found and watched a streamer; break inner loop
        else:
            print("Unknown command. Try 'open' or 'exit'.")
        print()


if __name__ == "__main__":
    main()
