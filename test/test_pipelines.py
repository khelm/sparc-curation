import unittest
from pathlib import Path
import pytest
from sparcur import pipelines as pipes


class TestApiNAT(unittest.TestCase):

    source = Path('~/ni/sparc/apinat/sources/').expanduser()  # FIXME config probably

    def test_load(self):
        m = pipes.ApiNATOMY(Path(self.source, 'apinatomy-model.json'))
        # FIXME I think only the model conforms to the schema ?
        #g = pipes.ApiNATOMY(Path(self.source, 'apinatomy-generated.json'))
        #rm = pipes.ApiNATOMY(Path(self.source, 'apinatomy-resourceMap.json'))
        m.data.keys()
        #asdf = m.data.keys(), g.data.keys(), rm.data.keys()

    @pytest.mark.skip('hardcoded assumptions mean this does not work yet')
    def test_export_model(self):
        m = pipes.ApiNATOMY(Path(self.source, 'apinatomy-model.json'))
        # FIXME need a way to combine this that doesn't require
        # the user to know how to compose these, just send a message
        # to one of them they should be able to build the other from
        # the information at hand
        r = pipes.ApiNATOMY_rdf(m)
        r.data

    def test_export_rm(self):
        rm = pipes.ApiNATOMY(Path(self.source, 'apinatomy-resourceMap.json'))
        r = pipes.ApiNATOMY_rdf(rm)  # FIXME ... should be able to pass the pipeline
        r.data
        #breakpoint()
