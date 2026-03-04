# The goal

I want to use rclone to keep in two way sync a target folder on a samba share (remote) and one on this machine (local).

The bisync job should run at regular interval.

The remote could be unreliable at times (it disconnects)

I wish to have a UI where the user can:

- specify the path of the remote folder to keep in sync
- specify the path of the local folder to keep in sync
    - an init step that will take care of creating the local path if it does not exist and do the first run with resync

The UI should present the pairs source/destination folders as a list that can be run as task. At each run, the bisync is performed for each pair on the list.

User should be able to add/delete pairs from the list.

I would like to keep a log for the last task run, to be checked if needed. 

# test environment

- source on the remote: "T:\Users\test"
- destination on the local "D:Test"

