# modo: product brief

How modo should behave and what it should achieve. It describes behavior and outcomes, not technology or visual design.

## What modo is

modo is a map that shows a group of people where to meet so the driving is fair. You put in 2 to 8 starting points, and it shows the best meeting spots anywhere on the road network. You pick one and see everyone's route there.

- **One way to be fair, for now:** the spot where the *longest* drive is shortest (minimax). Least total driving could be added later if it would help.
- **Anywhere, exactly:** a meeting spot can be any point on a road. modo shows the true best spot, even mid-highway or at an intersection, and never nudges it somewhere else.
- **Close enough is enough:** every spot within about a minute of the best counts as a good answer. These form stretches of road rather than single points, usually one small patch, sometimes a few. All of them show on the map, with no limit.
- **Where:** the US for the first public release. The rest of the world is a goal for later.
- **Two separate ideas, never mixed:** finding candidate spots, and drawing routes to the *one* spot the user picks. Routing to every candidate at once would be a mess.

## What the user does and sees

1. **Add a starting point.** For each point, the user can type an address or tap the map, and switch between the two smoothly. The point appears on the map in its own color.
2. **Add a second one.** Straight away, modo lights up every near-best stretch of road on the map and puts one marker on the exact best spot of each separate patch. There's no "go" button.
3. **Keep adding, up to 8.** Each new point refreshes the candidates. The map zooms to keep every point and candidate in view.
4. **Look at the candidates.** Hovering over a marker, or anywhere on a lit-up road, shows a small pop-up with the drive time from every starting point to that spot. On a phone, the first tap shows the times. The times come from the same search that found the candidates, so there's no extra wait.
5. **Pick a spot.** Clicking a marker or anywhere on a lit-up road (on a phone, a second tap) draws a route from every starting point to that spot, along real roads, each in its starting point's color, with each person's drive time. If there's only one patch, its best spot's routes show without a pick.
6. **Change anything.** Adding, moving or removing a point clears the routes and refreshes the candidates.
7. **Share it.** The page's link reopens the same group. The locations live only in the link, never on a server.

While work is running, the map shows progress. Candidates appear as they are found, rather than after a long blank wait.

## Example cases

| Case | What should happen |
| --- | --- |
| Two friends across one city | A few spots near the middle of their drives, answered almost instantly. |
| Seattle, New York, Miami and LA | A real answer somewhere in the middle of the country, with each person's drive time. No distance limit. |
| 8 people spread over one state | Candidates refresh smoothly as each person is added. |
| A point on an island or with no road route to the others | modo says plainly that there's no road route, rather than guessing. |
| The best spot is in the middle of a highway | That exact spot is the answer. It is never nudged. |
| Two different highways are about equally good | Both places show, so the user can choose. |

## What good means

| Quality | Target |
| --- | --- |
| Speed | Feels near-instant. Never more than 1 minute, and kept as low as possible. Progress is always visible. |
| Candidate accuracy | Every spot shown is within about 1 minute of the true best. |
| Drive-time accuracy | Within about 2 minutes on short drives and 5% on long ones. |
| Roads | Every drivable road. Car, no traffic. |
| Phones | Smooth on a phone. |
| Cost | $0 preferred. Up to $20 a month only when there's no other way. |
| Privacy | No accounts, no tracking, no stored starting points. |
| Openness | Everything open source. |

## Not in scope

- Live traffic or time-of-day drive times.
- Least total driving, for now.
- Nudging the best spot to somewhere you can stop, for now.
- Anywhere outside the US, for the first public release.
- Suggesting venues such as restaurants or parks at the meeting spot.
- Walking, cycling or transit.
- Accounts or saved groups.
