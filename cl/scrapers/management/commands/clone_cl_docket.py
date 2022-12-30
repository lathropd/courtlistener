"""
This tool allows you to partially clone a docket from courtlistener.com to your
local environment, you only need to pass the docket_id and run it.

e.g.

docker-compose -f docker/courtlistener/docker-compose.yml exec cl-django python manage.py cl_clone_docket --docket 66691997

This tool is only for development purposes, to enable it you need to set
environment variable DEVELOPMENT to True. Also need to set CL_API_TOKEN env
variable.

This is still work in progress, some data is not cloned yet.

"""
import os

import environ
import requests
from django.db import transaction
from django.utils.dateparse import parse_date

from cl.lib.command_utils import VerboseCommand
from cl.people_db.models import Person
from cl.search.models import Docket, Court

env = environ.FileAwareEnv()
DEVELOPMENT = env.bool("DEVELOPMENT", default=False)


def get_court(court_url: str) -> Court | None:
    """Get court from url
    :param court_url: court url from courtlistener.com
    :return: Court or None
    """
    if not court_url:
        return None
    s = requests.session()
    s.headers = {
        "Authorization": "Token %s" % os.environ.get("CL_API_TOKEN", "")}
    court_data = s.get(court_url).json()
    # delete resource_uri value generated by DRF
    del court_data["resource_uri"]

    try:
        ct = Court.objects.get_or_create(**court_data)
    except:
        ct = Court.objects.filter(pk=court_data['id'])[0]

    return ct


def get_person(person_url: str) -> Person | None:
    """Get person from url
    :param person_url: person url from courtlistener.com
    :return: Person or None
    """
    if not person_url:
        return None
    s = requests.session()
    s.headers = {
        "Authorization": "Token %s" % os.environ.get("CL_API_TOKEN", "")}
    person_data = s.get(person_url).json()
    # delete resource_uri value generated by DRF
    del person_data["resource_uri"]
    # delete fields with fk or m2m relations or unneeded fields
    # TODO create helpers to build that objects
    del person_data["aba_ratings"]
    del person_data["race"]
    del person_data["sources"]
    del person_data["educations"]
    del person_data["positions"]
    del person_data["political_affiliations"]
    # Prepare some values
    person_data["date_dob"] = parse_date(person_data["date_dob"])
    try:
        person, created = Person.objects.get_or_create(**person_data)
    except:
        person = Person.objects.filter(pk=person_data['id'])[0]

    return person


def get_docket_data(docket_id: int) -> None:
    """Download docket data from courtlistener.com and add to local version
    :param docket_id: docket id to clone
    :return: None
    """
    try:
        docket_obj = Docket.objects.get(pk=docket_id)
        print(f"Docket with id: {docket_id} already in local env.")
        return
    except Docket.DoesNotExist:
        # Create new Docket

        s = requests.session()
        s.headers = {
            "Authorization": "Token %s" % os.environ.get("CL_API_TOKEN", "")}
        docket_endpoint = f"https://www.courtlistener.com/api/rest/v3/dockets/{docket_id}/"
        docket_data = s.get(docket_endpoint).json()

        # Remove unneeded fields
        del docket_data["resource_uri"]
        del docket_data["original_court_info"]
        del docket_data["absolute_url"]
        # TODO helpers to create other objects and set m2m relations
        del docket_data["clusters"]
        del docket_data["audio_files"]
        del docket_data["tags"]
        del docket_data["panel"]

        with transaction.atomic():
            docket_data["court"] = get_court(docket_data['court'])
            docket_data["appeal_from"] = get_court(docket_data['appeal_from'])
            docket_data["assigned_to"] = get_person(docket_data['assigned_to'])
            Docket.objects.create(**docket_data)
            print(
                f"http://localhost:8000/docket/{docket_data['id']}/{docket_data['slug']}/")


class Command(VerboseCommand):
    help = "A helper function clone a docket from courtlistener.com"

    def add_arguments(self, parser):
        parser.add_argument(
            "--docket_id",
            help="docket id, "
                 "docket id from courtlistener.com eg. 14614371 from https://www.courtlistener.com/docket/14614371/smith/",
        )

    def handle(self, *args, **options):

        if DEVELOPMENT:
            docket_id = options["docket_id"]
            if docket_id:
                get_docket_data(docket_id)
        else:
            print("Command not enabled for production environment")
