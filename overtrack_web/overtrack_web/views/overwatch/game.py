import collections
import hashlib
import json
import logging
import os
import string
import warnings
from functools import reduce
from typing import List, Optional, Sequence, Tuple, Iterable
from urllib.parse import urlparse

import boto3
import requests
from dataclasses import asdict, fields, is_dataclass
from flask import Blueprint, Request, render_template, request, url_for, render_template_string, Response
from itertools import chain

from overtrack_models.dataclasses.overwatch.basic_types import Map
from overtrack_models.dataclasses.overwatch.overwatch_game import OverwatchGame
from overtrack_models.dataclasses.overwatch.performance_stats import HeroStats
from overtrack_models.dataclasses.overwatch.teamfights import PlayerStats
from overtrack_models.dataclasses.overwatch.teams import Player
from overtrack_models.dataclasses.typedload import referenced_typedload
from overtrack_models.orm.overwatch_game_summary import OverwatchGameSummary
from overtrack_web.data import overwatch_data
from overtrack_web.data.overwatch_data import hero_colors
from overtrack_web.lib.authentication import check_authentication
from overtrack_web.lib.opengraph import Meta
from overtrack_web.lib.overwatch_legacy import get_legacy_paths
from overtrack_web.lib.session import session
from overtrack_web.views.overwatch import OLDEST_SUPPORTED_GAME_VERSION, sr_change
from overtrack_web.views.overwatch.games_list import map_thumbnail_style

GAMES_BUCKET = 'overtrack-overwatch-games'
COLOURS = {
    'WIN': '#62c462',
    'LOSS': '#ee5f5b',
    'DRAW': '#f89406',
}
LEGACY_URL = 'https://overtrack.gg'


request: Request = request

logger = logging.getLogger(__name__)

try:
    s3 = boto3.client('s3')
    """ :type s3: mypy_boto3.s3.Client """
    s3.list_objects_v2(Bucket=GAMES_BUCKET)
except:
    logger.exception('Failed to create AWS S3 client - using HTTP for downloading games')
    s3 = None
try:
    logs = boto3.client('logs')
    """ :type s3: boto3_type_annotations.s3.Client """
except:
    logger.exception('Failed to create AWS logs client - running without admin logs')
    logs = None

try:
    legacy_scripts, legacy_stylesheet = get_legacy_paths(LEGACY_URL)
except:
    logger.exception('Failed to get legacy script paths')
    legacy_scripts, legacy_stylesheet = {}, None

game_blueprint = Blueprint('overwatch.game', __name__)


@game_blueprint.route('/<path:key>')
def game(key: str):
    try:
        summary = OverwatchGameSummary.get(key)
    except OverwatchGameSummary.DoesNotExist:
        return 'Game does not exist', 404

    if summary.player_name and summary.result != 'UNKNOWN':
        title = f'{summary.player_name}\'s {summary.result} on {summary.map}'
    elif summary.player_name:
        title = f'{summary.player_name}\'s game on {summary.map}'
    else:
        title = f'{key.split("/", 1)[0]}\'s game on {summary.map}'

    if not summary.game_version or summary.game_version < OLDEST_SUPPORTED_GAME_VERSION or 'legacy' in request.args:
        return render_template(
            'overwatch/game/legacy_game.html',
            title=title,
            legacy_base=LEGACY_URL,
            legacy_scripts=legacy_scripts,
            legacy_stylesheet=legacy_stylesheet,
        )

    game = load_game(summary)
    game.timestamp = summary.time

    if game.teams.owner and game.result != 'UNKNOWN':
        title = f'{game.teams.owner.name}\'s {game.result} on {game.map.name}'
    elif game.teams.owner:
        title = f'{game.teams.owner.name}\'s game on {game.map.name}'

    dev_info = get_dev_info(summary, game)

    imagehash = hashlib.md5(str((game.result, game.start_sr, game.end_sr)).encode()).hexdigest()

    try:
        tfs = game.teamfights
        stat_totals = {
            'eliminations.during_fights': len(tfs.eliminations_during_fights),
            'deaths.during_fights': len(tfs.eliminations_during_fights) + len(tfs.suicides_during_fights),
            'killfeed_assists.during_fights': len(tfs.killfeed_assists_during_fights),
            'first_elims': len(tfs.first_bloods),
            'first_deaths': len(tfs.first_bloods),
            'eliminations.outside_fights': len(tfs.eliminations_outside_fights),
            'deaths.outside_fights': len(tfs.eliminations_outside_fights) + len(tfs.suicides_outside_fights),
            'killfeed_assists.outside_fights': len(tfs.killfeed_assists_outside_fights),
            'fight_starts_missed': len(tfs.teamfights),
            'times_staggered': len(tfs.teamfights),
        }
        show_stats = True
    except AttributeError:
        stat_totals = {}
        show_stats = False

    def process_stat(
        stats: PlayerStats,
        field: str,
        category: str,
        role: int,
        view: str,
        percent: bool = False,
    ) -> Tuple[str, str]:
        def build_attr(stats, field):
            parts = field.split('.')
            try:
                return reduce(getattr, parts, stats)
            except AttributeError:
                return None

        def for_attr(field: str) -> Iterable[int]:
            if category == 'player':
                blue = game.teams.blue
                red = game.teams.red
                all_stats = [x.stats for x in blue + red]
            else:
                blue = game.teamfights.team_stats[0]
                red = game.teamfights.team_stats[1]
                all_stats = [blue, red]
            return [
                x for x in (build_attr(x, field) for x in all_stats)
                if x is not None
            ]

        stat = build_attr(stats, field)
        if stat is None:
            return 'stat-below-threshold', ''

        try:
            if view == 'per-teamfight':
                if percent:
                    value = f'{(stat / stats.teamfights) * 100:.1f} %'
                else:
                    value = f'{stat / stats.teamfights:.2f}'
            elif view == 'per-10min':
                value = f'{stat / (stats.playtime / 600):.2f}'
            elif view == 'ratio' and stat != 0:
                total = stat_totals[field]
                value = f'{100 * stat / total:.1f}'
            elif view == 'ratio':
                value = '0'
            else:
                value = str(stat)
        except ZeroDivisionError:
            return 'stat-below-threshold', ''

        values = for_attr(field)
        if not values:
            values = [stat]

        min_val = min(values)
        max_val = max(values)
        threshold = max_val - min_val > max_val / 2 > 1

        if stat == 0:
            stat_display = 'stat-zero'
        elif threshold:
            percent = (stat - min_val) / (max_val - min_val)
            if percent > 0.9:
                stat_display = 'stat-very-high'
            elif percent > 0.6:
                stat_display = 'stat-high'
            elif percent >= 0.4:
                stat_display = 'stat-median'
            elif percent >= 0.1:
                stat_display = 'stat-low'
            else:
                stat_display = 'stat-very-low'
        else:
            stat_display = 'stat-below-threshold'

        if any(x in field for x in ['kill', 'elim']) and 'low' in stat_display and role is not None and role >= 4:
            # don't consider a support with low kills as doing bad
            stat_display = 'stat-below-threshold'

        stat_display += f' stat-{field} role-{role}'

        if any(
            x in field
            for x in ['deaths', 'missed', 'suicides', 'staggered']
        ):
            stat_display += ' stat-red-highs'
        else:
            stat_display += ' stat-green-highs'

        return stat_display, value

    def get_top_heroes(player: Player) -> List[Tuple[str, PlayerStats]]:
        heroes = sorted(
            [(n, s) for n, s in player.stats_by_hero.items() if s.playtime > 60],
            key=lambda x: x[1].playtime,
            reverse=True
        )

        return heroes[:3]

    def get_hero_image(name: str) -> str:
        return f'images/overwatch/hero_icons/{name}.png'

    def get_hero_color(name: str) -> str:
        try:
            return hero_colors[name]
        except KeyError:
            return '#5d518e'

    return render_template(
        'overwatch/game/game.html',

        title=title,
        meta=Meta(
            title=title,
            image_url=url_for('overwatch.game.game_card_png', key=key, _external=True) + f'?_cachebust={imagehash}',
            twitter_image_url=url_for('overwatch.game.game_card_png', key=key, _external=True) + f'?height=190&_cachebust={imagehash}',
            summary_large_image=True,
            colour=COLOURS.get(game.result, 'gray')
        ),

        show_stats=show_stats,
        get_top_heroes=get_top_heroes,
        get_hero_color=get_hero_color,
        get_hero_image=get_hero_image,
        process_stat=process_stat,

        summary=summary,
        game=game,

        dev_info=dev_info,

        OLDEST_SUPPORTED_GAME_VERSION=OLDEST_SUPPORTED_GAME_VERSION,
    )


@game_blueprint.route('<path:key>/card')
def game_card(key: str):
    try:
        game = OverwatchGameSummary.get(key)
    except OverwatchGameSummary.DoesNotExist:
        return 'Game does not exist', 404

    return render_template_string(
        '''
            <!DOCTYPE html>
            <html lang="en">
                <head>
                    <title>{{ title }}</title>
                    <link rel="stylesheet" type="text/css" href="{{ url_for('static', filename='css/' + game_name + '.css') }}">
                    <style>
                        body {
                            background-color: rgba(0, 0, 0, 0);
                        }
                        .game-summary {
                            margin: 0 !important;
                        }
                    </style>
                </head>
                <body>
                    {% include 'overwatch/games_list/game_card.html' %}
                </body>
            </html>
        ''',
        title='Card',
        game=game,
        show_rank=True,
        OLDEST_SUPPORTED_GAME_VERSION=OLDEST_SUPPORTED_GAME_VERSION,

        map_thumbnail_style=map_thumbnail_style,
    )


@game_blueprint.route('/<path:key>/card.png')
def game_card_png(key: str):
    if 'RENDERTRON_URL' not in os.environ:
        return 'Rendertron url not set on server', 500

    url = ''.join((
        os.environ['RENDERTRON_URL'],
        'screenshot/',
        url_for('overwatch.game.game_card', key=key, _external=True),
        f'?width={min(int(request.args.get("width", 356)), 512)}',
        f'&height={min(int(request.args.get("height", 80)), 512)}',
        f'&_cachebust={request.args.get("_cachebust", "")}'
    ))
    try:
        r = requests.get(
            url,
            timeout=15,
        )
    except:
        logger.exception('Rendertron failed to respond within the timeout')
        return 'Rendertron failed to respond within the timeout', 500
    try:
        r.raise_for_status()
    except:
        logger.exception('Rendertron encountered an error fetching screenshot')
        return f'Rendertron encountered an error fetching screenshot: got status {r.status_code}', 500

    return Response(
        r.content,
        headers=dict(r.headers)
    )


@game_blueprint.context_processor
def context_processor():
    return {
        'game_name': 'overwatch',

        'sr_change': sr_change,

        'ability_is_ult': ability_is_ult,
        'get_ult_ability': get_ult_ability,

        'sort_stats': sort_stats,
        'get_stat_type': get_stat_type,

        'asdict': asdict,
    }


def ability_is_ult(ability) -> bool:
    if not ability:
        return False
    hero_name, ability_name = ability.split('.')
    hero = overwatch_data.heroes.get(hero_name)

    if ability_name in ['high_noon', 'dragon_blade']:
        # legacy ability names
        return True
    if not hero:
        warnings.warn(f'Could not get hero data for {hero_name}', RuntimeWarning)
        return False
    if not hero.ult:
        warnings.warn(f'Hero {hero_name} does not have ult defined', RuntimeWarning)
        return False
    return ability_name == hero.ult


def get_ult_ability(hero):
    if not hero or hero not in overwatch_data.heroes:
        return None
    return f'{hero}.{overwatch_data.heroes.get(hero).ult}'


def sort_stats(stats: Sequence[HeroStats]) -> List[HeroStats]:
    stats = sorted(list(stats), key=lambda s: (s.hero != 'all heroes', s.hero))
    heroes = [s.hero for s in stats]
    if 'all heroes' in heroes and len(heroes) == 2:
        # only 'all heroes' and one other
        stats = [s for s in stats if s.hero != 'all heroes']
    else:
        stats = [s for s in stats if s.time_played > 60]

    return stats


def get_stat_type(hero_name: str, stat_name: str) -> str:
    if hero_name in overwatch_data.heroes:
        hero = overwatch_data.heroes[hero_name]
        stats_by_name = {
            s.name: s
            for s in chain(*hero.stats)
        }
        if stat_name in stats_by_name:
            stat = stats_by_name[stat_name]
            if stat.stat_type == 'maximum':
                return 'value'
            if stat.stat_type == 'average':
                return 'average'
            elif stat.stat_type == 'best':
                return 'best'
            else:
                logger.error(f"Don't know how to handle stat type {stat.stat_type!r} for {hero_name}: {stat_name!r}")
                return 'value'

    logger.error(f"Couldn't get stat type for {hero_name}: {stat_name!r}")
    return 'value'


@game_blueprint.app_template_filter('map_jumbo_style')
def map_jumbo_style(map_: Map):
    map_name = map_.name.lower().replace(' ', '-')
    map_name = ''.join(c for c in map_name if c in (string.digits + string.ascii_letters + '-'))
    return (
        f'background-image: linear-gradient(#0d1235 0px, rgba(0, 0, 0, 0.42) 100%), '
        f'url({url_for("static", filename="images/overwatch/map_banners/" + map_name + ".jpg")}); '
        f'background-color: #0d1235;'
    )


@game_blueprint.app_template_filter('format_number')
def format_number(v: Optional[float], precision: Optional[float] = 1):
    if v is None:
        return v
    if precision is None:
        if v > 500:
            precision = 0
        else:
            precision = 1
    return f'{v:,.{precision}f}'


@game_blueprint.app_template_filter('hero_name')
def hero_name(h: str):
    if h in overwatch_data.heroes:
        return overwatch_data.heroes[h].name
    else:
        return h.title()


def load_game(summary: OverwatchGameSummary) -> OverwatchGame:
    try:
        game_object = s3.get_object(
            Bucket=GAMES_BUCKET,
            Key=summary.key + '.json'
        )
        game_data = json.loads(game_object['Body'].read())
    except:
        if s3:
            logger.exception('Failed to fetch game data from S3 - trying HTTP')
        r = requests.get(f'https://overtrack-overwatch-games.s3.amazonaws.com/{summary.key}.json')
        r.raise_for_status()
        game_data = r.json()

    return referenced_typedload.load(game_data, OverwatchGame)


def get_dev_info(summary, game):
    if check_authentication() is not None or not session.user.superuser:
        return None

    summary_dict = summary.asdict()
    summary_dict['key'] = (summary.key, f'https://overtrack-overwatch-games.s3.amazonaws.com/{summary.key}.json')
    log_id = summary_dict['log_id']
    if log_id and len(log_id) == 3 and all(log_id):
        summary_dict['log_id'] = (
            ' '.join(log_id),
            (
                f'https://us-west-2.console.aws.amazon.com/cloudwatch/home?region=us-west-2#logEventViewer:'
                f'group={log_id[0]};'
                f'stream={log_id[1]};'
                f'start={log_id[2]}'
            )
        )
    try:
        frames_url = urlparse(summary.frames_uri)
        # noinspection PyNoneFunctionAssignment
        signed_url = s3.generate_presigned_url(
            'get_object',
            Params={
                'Bucket': frames_url.netloc,
                'Key': frames_url.path[1:]
            }
        )
        summary_dict['frames_uri'] = (summary.frames_uri, signed_url)
    except:
        pass

    def _quickdump(val):
        if isinstance(val, (tuple, str)):
            return val
        elif isinstance(val, collections.Sequence):
            return f'... {len(val)} items'
        else:
            rep = repr(val)
            if len(rep) < 50:
                return rep
            else:
                return rep[:50 - 3] + '...'

    game_dict = {}
    extras = {}
    for f in fields(game):
        if f.name == 'images':
            game_dict['images'] = ''
            for image in game.images:
                game_dict['\u00a0' * 6 + '.' + image] = (game.images[image].rsplit('/', 1)[-1], game.images[image])
        elif not is_dataclass(getattr(game, f.name)):
            game_dict[f.name] = getattr(game, f.name)
        else:
            dc = getattr(game, f.name)
            dc_data = []
            extras[f'Game.{f.name}'] = dc_data
            for ff in fields(dc):
                val = getattr(getattr(game, f.name), ff.name)
                if is_dataclass(val):
                    dc_data.append((ff.name, ''))
                    for fff in fields(val):
                        dc_data.append(('\u00a0' * 6 + '.' + fff.name, _quickdump(getattr(val, fff.name))))
                elif isinstance(val, list) and len(val) <= 6:
                    dc_data.append((ff.name, ''))
                    for i, v in enumerate(val):
                        dc_data.append(('\u00a0' * 6 + '.' + str(i), _quickdump(v)))
                else:
                    dc_data.append((ff.name, _quickdump(val)))

    game_dict['key'] = (summary.key, f'https://overtrack-overwatch-games.s3.amazonaws.com/{summary.key}.json')

    return {
        'Summary': list(summary_dict.items()),
        'Game': list(game_dict.items()),
        'Extras': extras,
    }
